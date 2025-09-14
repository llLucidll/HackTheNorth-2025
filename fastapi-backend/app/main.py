# main.py
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic_settings import BaseSettings
import httpx
from typing import List, Optional

# =========================================
# Settings (auto-loads from .env)
# =========================================
class Settings(BaseSettings):
    # e.g. "your-store.myshopify.com" (NO https://, NO trailing slash)
    shopify_store_domain: str = "example.myshopify.com"
    # Storefront API token from Shopify app (Storefront integration)
    shopify_storefront_token: str = ""
    # Match the API version you enabled for your app
    shopify_api_version: str = "2024-07"
    # HTTP client timeout
    request_timeout_seconds: float = 8.0

    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()

def normalize_domain(domain: Optional[str]) -> str:
    d = (domain or "").strip()
    if d.startswith("http://"):
        d = d[len("http://"):]
    if d.startswith("https://"):
        d = d[len("https://"):]
    d = d.strip().strip("/")
    return d

def ensure_config_ok() -> str:
    d = normalize_domain(settings.shopify_store_domain)
    if not d or "." not in d:
        raise RuntimeError(
            "Invalid SHOPIFY_STORE_DOMAIN. Expected 'myshop.myshopify.com' "
            "(no protocol, no trailing slash)."
        )
    if not settings.shopify_storefront_token:
        raise RuntimeError("Missing SHOPIFY_STOREFRONT_TOKEN.")
    return d

# =========================================
# FastAPI app
# =========================================
app = FastAPI(title="FastAPI Shopify Search", version="0.1.0")

# CORS (tighten in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def _startup_check():
    # Validate config early; log readable errors
    try:
        ensure_config_ok()
    except Exception as e:
        # You can raise here to stop the app if configuration is wrong.
        # For dev, we just print; uncomment raise to hard-fail.
        print(f"[CONFIG WARNING] {e}")
        # raise

# =========================================
# Models
# =========================================
class Money(BaseModel):
    amount: float
    currencyCode: str

class ProductResult(BaseModel):
    id: str
    title: str
    handle: str
    url: Optional[str] = None
    price: Optional[Money] = None
    image: Optional[str] = None
    imageAlt: Optional[str] = None

class SearchResponse(BaseModel):
    store: str
    query: str
    count: int
    results: List[ProductResult]

# =========================================
# Shopify Storefront API Client
# =========================================
GRAPHQL_QUERY = """
query ProductSearch($q: String!, $num: Int!) {
  products(first: $num, query: $q, sortKey: RELEVANCE) {
    edges {
      node {
        id
        title
        handle
        onlineStoreUrl
        featuredImage { url altText }
        images(first: 1) { edges { node { url altText } } }
        variants(first: 1) {
          edges {
            node {
              price { amount currencyCode }
              priceV2 { amount currencyCode }
            }
          }
        }
      }
    }
  }
}
"""

async def search_shopify_products(
    query: str,
    limit: int = 5,
    store_domain: Optional[str] = None,
) -> List[ProductResult]:
    # Use override store if provided; otherwise, validate default config
    domain = normalize_domain(store_domain) if store_domain else ensure_config_ok()
    token = settings.shopify_storefront_token.strip()
    if not token:
        raise HTTPException(
            status_code=500,
            detail="Missing SHOPIFY_STOREFRONT_TOKEN. Set it in your .env."
        )

    url = f"https://{domain}/api/{settings.shopify_api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": token,
    }
    variables = {"q": query, "num": max(1, min(limit, 50))}

    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        resp = await client.post(url, headers=headers, json={"query": GRAPHQL_QUERY, "variables": variables})
        if resp.status_code != 200:
            # Try to surface Shopify error payload
            try:
                err = resp.json()
            except Exception:
                err = resp.text
            raise HTTPException(status_code=502, detail={"message": "Shopify request failed", "error": err})

        data = resp.json()
        if "errors" in data:
            raise HTTPException(status_code=502, detail={"message": "Shopify GraphQL error", "error": data["errors"]})

        edges = data.get("data", {}).get("products", {}).get("edges", [])
        results: List[ProductResult] = []
        for edge in edges:
            node = edge.get("node", {})

            # price: prefer price (new) then priceV2 (old)
            variant_edges = node.get("variants", {}).get("edges", [])
            price_node = None
            if variant_edges:
                v = variant_edges[0].get("node", {})
                price_node = v.get("price") or v.get("priceV2")

            # image: featuredImage then first image
            featured = node.get("featuredImage") or {}
            img_edges = node.get("images", {}).get("edges", [])
            img_node = featured or (img_edges[0]["node"] if img_edges else {})

            results.append(ProductResult(
                id=node.get("id", ""),
                title=node.get("title", ""),
                handle=node.get("handle", ""),
                url=node.get("onlineStoreUrl"),
                price=Money(
                    amount=float(price_node["amount"]),
                    currencyCode=price_node["currencyCode"]
                ) if price_node else None,
                image=img_node.get("url"),
                imageAlt=img_node.get("altText")
            ))

        return results

# =========================================
# Routes
# =========================================
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}

@app.get("/", tags=["meta"])
def root():
    return {"message": "Hello from FastAPI!"}

@app.get("/_config", tags=["meta"])
def _config():
    # helps debug domain & token presence (doesn't leak token)
    return {
        "shopify_store_domain": normalize_domain(settings.shopify_store_domain),
        "shopify_api_version": settings.shopify_api_version,
        "token_present": bool(settings.shopify_storefront_token),
    }

@app.get("/search", response_model=SearchResponse, tags=["shopify"])
async def search(
    q: str = Query(..., description="Search query, e.g., 'BLACK NIKE SHOES'"),
    limit: int = Query(5, ge=1, le=50),
    store: Optional[str] = Query(None, description="Override store domain, e.g., 'myshop.myshopify.com'"),
):
    """
    Search products on a Shopify store via Storefront GraphQL API.
    Returns top N results (default 5) by relevance in a simplified list.
    """
    results = await search_shopify_products(query=q, limit=limit, store_domain=store)
    return SearchResponse(
        store=normalize_domain(store) if store else normalize_domain(settings.shopify_store_domain),
        query=q,
        count=len(results),
        results=results
    )
