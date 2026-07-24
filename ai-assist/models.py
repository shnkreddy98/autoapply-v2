from pydantic import BaseModel, Field
from typing import Literal, Optional
from datetime import date, datetime


class Contact(BaseModel):
    name: str
    email: str
    location: str
    phone: str
    country_code: str = "+1"
    linkedin: str = ""
    github: str = ""


class Education(BaseModel):
    degree: str
    major: str
    college: str
    from_: date | None = Field(alias="from_date", default=None)
    to_: date | str | None = Field(alias="to_date", default=None)

    class Config:
        populate_by_name = True


class JobExperience(BaseModel):
    job_title: str
    company_name: str
    location: str
    from_: date | None = Field(alias="from_date", default=None)
    to_: date | str | None = Field(alias="to_date", default=None)
    experience: list[str]

    class Config:
        populate_by_name = True


class Certification(BaseModel):
    title: str
    obtained_date: date | None = None
    expiry_date: date | None = None


class Skills(BaseModel):
    title: str
    skills: str


class Project(BaseModel):
    title: str = Field(description="Project title or name")
    description: str = Field(description="Project description and outcomes")
    technologies: list[str] = Field(default=[], description="Technologies used")
    start_date: date | None = Field(default=None)
    end_date: date | None = Field(default=None)


class Achievement(BaseModel):
    title: str = Field(description="Achievement or award title")
    description: str = Field(description="Details about the achievement")
    achieved_date: date | None = Field(default=None)


class Resume(BaseModel):
    contact: Contact
    summary: str
    job_exp: list[JobExperience]
    skills: list[Skills]
    education: list[Education]
    certifications: list[Certification]
    projects: list[Project] = Field(default=[])
    achievements: list[Achievement] = Field(default=[])


class TailoredResume(BaseModel):
    role: str = Field(description="Job role name")
    company_name: str = Field(description="Name of the company that posted the job")
    date_posted: Optional[datetime] = Field(
        description="Job posted date if mentioned", default=None
    )
    cloud: Literal["aws", "gcp", "azu"] = Field(
        description="The dominant/preferred cloud technology", default="aws"
    )
    resume_score: float = Field(
        description="Resume score on a scale of 0 to 100 BEFORE tailoring", le=100, ge=0
    )
    job_match_summary: str = Field(
        description="Explanation of how well the resume matches and what was changed"
    )
    new_resume_score: float = Field(
        description="Resume score AFTER tailoring changes", le=100, ge=0
    )


def get_gemini_compatible_schema(model: type[BaseModel]) -> dict:
    """
    Generates a JSON schema from a Pydantic model and recursively
    resolves $ref dependencies (Gemini API does not support $defs/$ref).
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref_key = node["$ref"].split("/")[-1]
                if ref_key in defs:
                    return resolve(defs[ref_key])
            return {k: resolve(v) for k, v in node.items()}
        elif isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


class QuestionAnswer(BaseModel):
    answer: str = Field(
        description="First-person answer to the application question, within the stated word limit, containing no em dashes"
    )
