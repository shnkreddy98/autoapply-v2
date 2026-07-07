"""Seed a user's full application profile from their resume, so the apply loop's
`build_profile` renders real values instead of `(not provided)`.

One-off, data hardcoded for the Shashank Reddy resume (ai-assist/data/shashank_reddy.pdf).
Idempotent: upserts users/application_profiles (1:1) and replaces the user's
work/education/skill rows. Run from auto-browse/:

    uv run python -m tools.seed_profile --user-id <uuid>

Outward-facing fields confirmed with the operator:
  email=shnkreddy98@gmail.com, work_eligible_us=true, visa_sponsorship=true,
  willing_relocate=true. Salary / start date / street address / DOB left NULL
  (render as blank; the agent leaves them empty or asks a human).
"""

from __future__ import annotations

import argparse

from common import db

EMAIL = "shnkreddy98@gmail.com"
FULL_NAME = "Shashank Shashishekhar Reddy"
PHONE = "(510) 892-7191"
LINKEDIN = "https://linkedin.com/in/shnkreddy"
GITHUB = "https://github.com/shnkreddy98"
LOCATION = "San Jose, CA"
SUMMARY = (
    "Data Engineer with 5+ years designing scalable data warehouses and product "
    "analytics solutions that drive business impact. Expert in building optimal data "
    "artifacts through ETL pipelines, SQL transformations, and data governance "
    "protocols using AWS, Spark, and Kafka. Proven ability to translate complex data "
    "challenges into actionable insights for product teams while ensuring data "
    "quality, security, and operational excellence."
)

WORK = [
    (
        "Founding Engineer", "AirFold", "San Francisco, California",
        "2025-01-01", None, True,
        [
            "Designed dimensional data models and logging schemas in collaboration with product and engineering teams, enabling efficient analytics queries and reducing time-to-insight for strategic business decisions.",
            "Engineered event-driven data ingestion systems using Kafka and Debezium CDC, processing millions of unstructured events with sub-second latency and exactly-once semantics.",
            "Developed modular API frameworks (FastAPI) with robust RBAC security protocols to support secure data access and integration for enterprise-scale applications.",
            "Implemented automated data governance workflows using dbt and Snowflake, creating validatable transformation logic that reduced reporting discrepancies by 40%.",
            "Optimized NoSQL and distributed database performance (DynamoDB, ClickHouse) to handle complex vector search retrieval and large-scale analytical queries.",
            "Tech Stack: AWS, GCP, Terraform, Kubernetes, Kafka, Debezium, ClickHouse, FastAPI, PostgreSQL, DynamoDB",
        ],
    ),
    (
        "Data Engineer", "Kantar", "Bengaluru, India",
        "2020-04-01", "2022-04-01", False,
        [
            "Orchestrated large-scale distributed data processing pipelines using Apache Spark and Airflow, ingesting terabytes of structured and unstructured data streams.",
            "Modernized legacy data models by deploying dbt for SQL transformations, establishing reliable data structures that improved analytical time-to-insight by 25%.",
            "Built fault-tolerant streaming architectures with Kafka and Spark Streaming to deliver near real-time operational intelligence and situational awareness.",
            "Automated data ingestion workflows using AWS Lambda and S3 serverless architecture, reducing manual overhead by 80% while ensuring data durability.",
            "Designed algorithmic optimization models to enhance resource allocation efficiency, driving significant performance improvements for global enterprise clients.",
            "Tech Stack: Apache Spark, Airflow, Kafka, AWS Lambda, S3, dbt, SQL, Python",
        ],
    ),
    (
        "Data Engineer", "The Sparks Foundation", "Bengaluru, India",
        "2018-04-01", "2020-03-01", False,
        [
            "Enhanced ETL pipeline performance by 25% through parallel processing techniques and query optimization to support high-volume data generation.",
            "Tuned complex SQL queries and database indexing strategies for PostgreSQL and MySQL, significantly reducing latency for downstream analytical reporting.",
            "Integrated automated data validation scripts to detect schema anomalies and null values, ensuring data integrity and minimizing downstream corruption incidents.",
            "Collaborated with stakeholders to design dimensional data models (Star Schema), facilitating efficient querying and supporting strategic decision-making processes.",
            "Tech Stack: Python, Pandas, PostgreSQL, MySQL, ETL, SQL, Git",
        ],
    ),
]

EDUCATION = [
    ("Master's Degree", "Data Analytics", "San Jose State University", "2023-01-01", "2024-12-01"),
    ("Post Graduate Diploma", "Data Science", "IIIT Bangalore", "2020-11-01", "2021-10-01"),
    ("Bachelor of Engineering", "Computer Science", "Visvesvaraya Technological University", "2016-07-01", "2020-08-01"),
]

SKILLS = [
    ("Languages", ["Python", "SQL", "Java", "Go", "Bash", "TypeScript"]),
    ("Big Data & Streaming", ["Spark", "PySpark", "Apache Kafka", "Airflow", "dbt", "Debezium CDC", "Ray Serve", "ETL/ELT", "Hadoop", "Hive"]),
    ("Cloud & Infrastructure", ["AWS (EC2, S3, Lambda, EKS, RDS)", "GCP", "Azure", "Terraform", "Kubernetes", "Docker", "Istio", "GitOps"]),
    ("Databases & Storage", ["Snowflake", "ClickHouse", "PostgreSQL", "DynamoDB", "Redshift", "MySQL", "Redis", "Delta Lake", "MongoDB", "Cassandra"]),
    ("DevOps & CI/CD", ["Git/GitHub Actions", "Pytest", "CI/CD Pipelines", "Agile Development", "Docker"]),
    ("Concepts & Protocols", ["FastAPI", "RESTful APIs", "Pandas", "NumPy", "Data Modeling", "Data Governance", "Distributed Systems", "Data Structuring"]),
]


def seed(conn, user_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET email=%s, full_name=%s, phone=%s, linkedin=%s, "
            "github=%s, location=%s, summary=%s, updated_at=now() WHERE user_id=%s",
            (EMAIL, FULL_NAME, PHONE, LINKEDIN, GITHUB, LOCATION, SUMMARY, user_id),
        )
        if cur.rowcount == 0:
            raise SystemExit(f"no users row for user_id={user_id} (seed the user first)")

        cur.execute(
            "INSERT INTO application_profiles "
            "(user_id, city, state, work_eligible_us, visa_sponsorship, security_clearance, "
            " employment_type, years_of_experience, willing_relocate, willing_travel, "
            " current_employee, ever_terminated, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            " city=EXCLUDED.city, state=EXCLUDED.state, "
            " work_eligible_us=EXCLUDED.work_eligible_us, "
            " visa_sponsorship=EXCLUDED.visa_sponsorship, "
            " security_clearance=EXCLUDED.security_clearance, "
            " employment_type=EXCLUDED.employment_type, "
            " years_of_experience=EXCLUDED.years_of_experience, "
            " willing_relocate=EXCLUDED.willing_relocate, "
            " willing_travel=EXCLUDED.willing_travel, "
            " current_employee=EXCLUDED.current_employee, "
            " ever_terminated=EXCLUDED.ever_terminated, updated_at=now()",
            (user_id, "San Jose", "CA", True, True, False,
             "Full-time", 5, True, True, False, False),
        )

        # Replace normalized resume rows (idempotent on rerun).
        for tbl in ("work_experiences", "education", "skill_groups"):
            cur.execute(f"DELETE FROM {tbl} WHERE user_id=%s", (user_id,))

        for i, (title, company, loc, frm, to, current, bullets) in enumerate(WORK):
            cur.execute(
                "INSERT INTO work_experiences "
                "(user_id, job_title, company_name, location, from_date, to_date, "
                " is_current, bullets, display_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, title, company, loc, frm, to, current, bullets, i),
            )
        for i, (degree, major, school, frm, to) in enumerate(EDUCATION):
            cur.execute(
                "INSERT INTO education "
                "(user_id, degree, major, school, from_date, to_date, display_order) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_id, degree, major, school, frm, to, i),
            )
        for i, (category, skills) in enumerate(SKILLS):
            cur.execute(
                "INSERT INTO skill_groups (user_id, category, skills, display_order) "
                "VALUES (%s,%s,%s,%s)",
                (user_id, category, skills, i),
            )
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True)
    args = ap.parse_args()
    with db.connect() as conn:
        seed(conn, args.user_id)
    print(f"seeded profile for user {args.user_id} ({EMAIL})")


if __name__ == "__main__":
    main()
