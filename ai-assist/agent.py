import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Type

import httpx
from pydantic import BaseModel

from custom_logging import get_logger
from env import LLM_BASE_URL, MODEL, OPENROUTER_API_KEY

get_logger()
logger = logging.getLogger(__name__)


class AgentResult(BaseModel):
    output: Any = None
    usage: Dict[str, int] = {}
    iterations: int = 0
    success: bool = True
    error: Optional[str] = None


class Agent:
    """
    Base agent for OpenRouter/OpenAI-compatible LLM APIs.

    Supports tool calling, structured JSON output, multi-turn conversation,
    and exponential-backoff retry. Context is auto-compressed after each
    tool-call iteration to keep token usage bounded.
    """

    def __init__(
        self,
        system_prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_functions: Optional[Dict[str, Callable]] = None,
        response_format: Optional[Type[BaseModel]] = None,
        model: str = MODEL,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tool_schemas: Optional[Dict[str, Type[BaseModel]]] = None,
    ):
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.tool_functions = tool_functions or {}
        self.tool_schemas = tool_schemas or {}
        self.response_format = response_format
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

        self.url = f"{LLM_BASE_URL}/chat/completions"
        self.headers = {"Content-Type": "application/json"}
        if OPENROUTER_API_KEY:
            self.headers["Authorization"] = f"Bearer {OPENROUTER_API_KEY}"

        self.messages: List[Dict[str, Any]] = []
        self.result = AgentResult()
        self.running = False
        self.stop_requested = False
        self.fatal_error: Optional[str] = None
        self.workflow_log: List[str] = []
        self._initial_query: str = ""

    def init_messages(self):
        system_content = self.system_prompt
        if self.response_format:
            schema = self.response_format.model_json_schema()
            if "title" in schema:
                del schema["title"]
            if "properties" in schema:
                for prop in schema["properties"].values():
                    if "title" in prop:
                        del prop["title"]
            system_content += (
                f"\n\nYou must respond with valid JSON matching this exact schema:\n"
                f"{json.dumps(schema, indent=2)}\n\n"
                "Return only the JSON object, no additional text."
            )
        self.messages = [{"role": "system", "content": system_content}]

    async def _call_llm_with_retry(
        self, payload: dict, max_retries: int = 3
    ) -> Optional[httpx.Response]:
        initial_delay = 2.0
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(
                        self.url, headers=self.headers, json=payload
                    )
                    if response.status_code == 200:
                        return response
                    elif response.status_code >= 500:
                        logger.warning(
                            f"Server error {response.status_code}, "
                            f"retrying ({attempt + 1}/{max_retries})..."
                        )
                    else:
                        # 4xx won't fix itself — bad key, bad model, bad payload.
                        # Flag it so run() aborts instead of looping on it.
                        logger.error(
                            f"API error {response.status_code}: {response.text}"
                        )
                        self.fatal_error = (
                            f"{self.url} returned {response.status_code}: "
                            f"{response.text[:300]}"
                        )
                        return None
            except (httpx.ReadError, httpx.ConnectError, httpx.ReadTimeout) as e:
                logger.warning(
                    f"Network error {type(e).__name__}, retrying ({attempt + 1}/{max_retries})..."
                )
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                return None

            if attempt < max_retries - 1:
                await asyncio.sleep(initial_delay * (2**attempt))

        logger.error("All retry attempts failed")
        return None

    def _tool_choice(self) -> str:
        return "auto"

    async def execute_tool(self, tool_name: str, arguments: dict) -> Any:
        if tool_name not in self.tool_functions:
            return {
                "error": f"Tool '{tool_name}' not found",
                "available_tools": list(self.tool_functions.keys()),
            }

        if tool_name in self.tool_schemas:
            try:
                validated_args = self.tool_schemas[tool_name](**arguments)
            except Exception as e:
                logger.warning(f"Validation failed for {tool_name}: {e}")
                return {
                    "error": f"Validation error for '{tool_name}': {e}\n\nCheck the tool schema and retry with correct argument names and types."
                }
        else:
            validated_args = arguments

        try:
            return await self.tool_functions[tool_name](validated_args)
        except Exception as e:
            logger.error(f"Error executing {tool_name}: {e}", exc_info=True)
            return {
                "error": f"Tool execution failed: {e}",
                "tool": tool_name,
                "type": type(e).__name__,
            }

    def _summarize_result(self, content: str, max_chars: int = 200) -> str:
        content = content.strip()
        return content if len(content) <= max_chars else content[:max_chars] + "…"

    def _compress_messages(self) -> None:
        if len(self.workflow_log) <= 1:
            return

        last_assistant_idx = None
        for i in range(len(self.messages) - 1, 0, -1):
            if self.messages[i].get("role") == "assistant" and self.messages[i].get(
                "tool_calls"
            ):
                last_assistant_idx = i
                break

        if last_assistant_idx is None or last_assistant_idx <= 1:
            return

        system_msg = self.messages[0]
        last_iter_msgs = self.messages[last_assistant_idx:]
        workflow_text = "Steps completed so far:\n" + "\n".join(
            f"  {i + 1}. {step}" for i, step in enumerate(self.workflow_log[:-1])
        )
        self.messages = [
            system_msg,
            {"role": "user", "content": f"{self._initial_query}\n\n{workflow_text}"},
            *last_iter_msgs,
        ]

    def _parse_structured_output(self, output: str) -> Any:
        parsed = output.strip()
        if "```" in parsed:
            start = parsed.find("{")
            end = parsed.rfind("}") + 1
            if start != -1 and end > start:
                parsed = parsed[start:end]
        elif not parsed.startswith("{"):
            start = parsed.find("{")
            end = parsed.rfind("}") + 1
            if start != -1 and end > start:
                parsed = parsed[start:end]
        data = json.loads(parsed)
        return self.response_format(**data)

    async def run(self, query: str, max_iterations: int = 50) -> AgentResult:
        self.stop_requested = False
        self.running = True
        self.result = AgentResult()
        self.fatal_error = None
        self._initial_query = query

        if not self.messages:
            self.init_messages()

        self.messages.append({"role": "user", "content": query})

        for iteration in range(max_iterations):
            if self.stop_requested:
                self.running = False
                self.result.success = False
                self.result.error = "Stopped by user"
                return self.result

            logger.info(f"Iteration {iteration + 1}/{max_iterations}")
            self.result.iterations = iteration + 1

            payload: dict = {
                "model": self.model,
                "messages": self.messages,
                "temperature": self.temperature,
            }
            if self.max_tokens:
                payload["max_tokens"] = self.max_tokens
            if self.tools:
                payload["tools"] = self.tools
                payload["tool_choice"] = self._tool_choice()
            if self.response_format:
                payload["response_format"] = {"type": "json_object"}

            response = await self._call_llm_with_retry(payload)
            if not response:
                if self.fatal_error:
                    logger.error(f"Aborting: {self.fatal_error}")
                    self.running = False
                    self.result.success = False
                    self.result.error = self.fatal_error
                    return self.result
                self.messages.append(
                    {"role": "user", "content": "Please continue with the next step."}
                )
                continue

            result = response.json()
            message = result.get("choices", [{}])[0].get("message", {})
            output = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []
            self.result.usage = result.get("usage", {})

            if not output and not tool_calls:
                self.messages.append(
                    {"role": "user", "content": "Please continue with the next step."}
                )
                continue

            self.messages.append(message)

            if not tool_calls:
                if self.response_format:
                    try:
                        self.result.output = self._parse_structured_output(output)
                    except Exception as e:
                        logger.error(f"Failed to parse structured output: {e}")
                        if output and (
                            '"type": "function"' in output
                            or ('"name":' in output and '"parameters":' in output)
                        ):
                            reprompt = "You returned a tool call in your text response instead of the required JSON. Output ONLY the JSON object matching the required schema."
                        else:
                            reprompt = f"Your response could not be parsed. Error: {e}\nOutput ONLY the JSON object matching the required schema."
                        self.messages.append({"role": "user", "content": reprompt})
                        continue
                else:
                    self.result.output = output

                self.running = False
                return self.result

            # Execute tool calls
            iteration_step_parts: List[str] = []
            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                tool_id = tool_call["id"]
                tool_args_raw = tool_call["function"]["arguments"]

                try:
                    tool_args = (
                        json.loads(tool_args_raw)
                        if isinstance(tool_args_raw, str) and tool_args_raw.strip()
                        else (tool_args_raw or {})
                    )
                except json.JSONDecodeError as e:
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": json.dumps({"error": f"Invalid JSON: {e}"}),
                        }
                    )
                    continue

                tool_result = await self.execute_tool(tool_name, tool_args)

                if isinstance(tool_result, dict) and "screenshot_base64" in tool_result:
                    tool_result = {
                        k: v for k, v in tool_result.items() if k != "screenshot_base64"
                    }

                content = (
                    json.dumps(tool_result)
                    if isinstance(tool_result, dict)
                    else str(tool_result)
                )
                self.messages.append(
                    {"role": "tool", "tool_call_id": tool_id, "content": content}
                )
                iteration_step_parts.append(
                    f"{tool_name} → {self._summarize_result(content)}"
                )

            if iteration_step_parts:
                self.workflow_log.append(" | ".join(iteration_step_parts))
                self._compress_messages()

        # Max iterations reached — try to salvage final assistant output
        logger.warning(f"Max iterations ({max_iterations}) reached")
        self.running = False

        if self.response_format:
            for msg in reversed(self.messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    try:
                        self.result.output = self._parse_structured_output(
                            msg["content"]
                        )
                        self.result.success = True
                        self.result.error = None
                        return self.result
                    except Exception as e:
                        logger.error(f"Failed to parse final output: {e}")
                    break

        self.result.success = False
        self.result.error = "Max iterations reached without valid output"
        return self.result

    def stop(self):
        self.stop_requested = True
