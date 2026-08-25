import asyncio
import httpx

async def run():
    url = "https://treasuremaps.net/api"
    params = {
        "apikey": "e87fde110a51068e37cf32befe42058c",
        "t": "movie",
        "o": "json",
        "q": "District 9"
    }
    async with httpx.AsyncClient(headers={"User-Agent": "NZBoxer/2.1.2 (Linux; x64)"}) as client:
        resp = await client.get(url, params=params)
        print("Status Code:", resp.status_code)
        print("Response Text:", resp.text)

asyncio.run(run())
