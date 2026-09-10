import os
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
r = client.search("FDIC deposit insurance limit per depositor", max_results=3)
for item in r["results"]:
    print(item["url"])