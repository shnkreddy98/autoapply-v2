import json
from pathlib import Path
from typing import Literal

import yaml

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


def main(search_term: str, location: str, duration: Literal["d", "h", "w", "m"] = "d"):
    url = "https://www.google.com/search?q={}&tbs=qdr:{}"

    links = read(filePath)
    links = links.split("\n")
    size = len(links)

    batch_size = 5
    batches = size // batch_size

    for batch in range(0, batches):
        batch_links = links[batch * batch_size : (batch + 1) * batch_size]
        batch_links = " OR ".join(batch_links)

        search = f'{search_term} "{location}" {batch_links}'
        q = search.replace(" ", "+")
        link = url.format(q, duration)
        print(link)


if __name__ == "__main__":
    role = "data engineer"
    location = "United States"
    duration = "d"
    main(role, location, duration)
