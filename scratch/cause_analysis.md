I have investigated the issue and found the cause.

The `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` is happening because the default `treasuremaps` API endpoint (`https://treasuremaps.net/api`) is returning an HTML page instead of JSON. 

When your application attempts to search for a movie, the server returns an `HTTP 200 OK` status, but the body of the response is a domain-parking page containing a JavaScript redirect rather than valid JSON:

```html
<!DOCTYPE html><html><head><script>window.onload=function(){window.location.href="/lander?apikey=...&o=json&q=District+9&t=movie"}</script></head></html>
```

Because `httpx` receives a `200` status code, it doesn't raise an HTTP error and tries to parse this HTML string using `response.json()`. This is what triggers the JSON parsing error in `app/services/treasure_maps.py`.

This indicates that the domain `treasuremaps.net` has likely expired, been parked, or is no longer functioning as an indexer. Since there are no custom indexer URLs set in your `providers` table, the app is falling back to this broken default URL.
