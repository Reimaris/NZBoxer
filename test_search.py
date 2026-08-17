import asyncio

from app.core.parser import parse_release_name
from app.core.scorer import score_release
from app.services import treasure_maps


async def main():
    # Use real api key
    print("Searching...")
    results = await treasure_maps.search_raw(query="star wars clone wars", season=1)
    print(f"Got {len(results)} raw results")

    for item in results:
        title = item.get("title", "")
        if not title:
            continue
        parsed = parse_release_name(title)

        # Simulating manual search logic
        is_season_pack = parsed.episode is None

        sr = score_release(
            parsed,
            size_bytes=item.get("size", 0),
            age_days=0,
            required_language=None,
            api_language=item.get("api_language"),
        )

        print(f"Title: {title}")
        print(
            f"Parsed EP: {parsed.episode} | Is Season Pack logic would keep it? {is_season_pack}"
        )
        print(f"Rejected by scorer? {sr.is_rejected} | Reason: {sr.reject_reason}")
        print("---")


asyncio.run(main())
