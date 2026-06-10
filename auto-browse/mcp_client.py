import asyncio

from mcp import ClientSession, ListToolsResult
from mcp.client.streamable_http import streamable_http_client


async def main():
    # Connect to a streamable HTTP server
    async with streamable_http_client("http://localhost:8931/mcp") as (
        read_stream,
        write_stream,
        _,
    ):
        # Create a session using the client streams
        async with ClientSession(read_stream, write_stream) as session:
            # Initialize the connection
            await session.initialize()
            # List available tools
            tools = await session.list_tools()

            for tool in tools.tools:
                print(f"Name: {tool.name}")
                print(f"Description: {tool.description}")
                print(f"Input Schema: {tool.inputSchema}")
                print(f"Output Schema: {tool.outputSchema}")
                print("---")


if __name__ == "__main__":
    asyncio.run(main())
