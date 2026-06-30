import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("BRAVE_API_KEY")

filePath = Path("data/unique_links.txt")


def read(file: Path):
    if not file.exists():
        print(f"Error: File not found, {file}")

    filepath = str(file)
    with open(filepath, "r") as f:
        if filepath.endswith(".json"):
            data = json.load(f)
        elif filepath.endswith(".yaml"):
            data = yaml.safe_load(f)
        else:
            data = f.read()
    return data


def generate_search_query(
    search_term: str,
    location: str,
    results_count: int = 20,
    duration: Literal["d", "h", "w", "m"] = "d",
    search_api: Literal["google", "brave"] = "brave",
):
    # TODO: Add google API handling
    google_url = "https://www.google.com/search?q={}&tbs=qdr:{}"
    brave_url = "https://api.search.brave.com/res/v1/web/search?q={}&freshness=p{}&result_filter=web&count={}"

    links = read(filePath)
    links = [l for l in links.split("\n") if l.strip()]  # filter empty lines

    result_links = []
    for site in links:
        search = f'{search_term} "{location}" site:{site}'
        q = quote_plus(search)
        if search_api == "brave":
            link = brave_url.format(q, duration, results_count)
        else:
            link = google_url.format(q, duration)
        result_links.append(link)

    return result_links


def brave_search(url: str):
    headers = {"X-Subscription-Token": api_key}
    print(f"url={url}")
    res_obj = requests.get(url=url, headers=headers)
    if 200 <= res_obj.status_code <= 299:
        response = res_obj.json()
        web = response.get("web")
        if not web:
            print(f"No web results. Query flagged: {response.get('query', {})}")
            return []
        return web.get("results", [])
    else:
        print(f"Failed with status code: {res_obj.status_code}")
        print(res_obj.text)
        return []


if __name__ == "__main__":
    role = "data engineer"
    location = "United States"
    duration = "d"

    links = generate_search_query(
        search_term=role, location=location, duration=duration
    )

    all_results = []
    for url in links:
        results = brave_search(url)
        if results:
            all_results.extend(results)

    print(f"\nTotal results: {len(all_results)}")
    for r in all_results:
        print(r.get("url"), "-", r.get("title"))

    from enqueue import enqueue_jobs

    job_ids = enqueue_jobs(all_results)
    print(f"\nEnqueued {len(set(job_ids))} unique jobs (jd_pending).")
