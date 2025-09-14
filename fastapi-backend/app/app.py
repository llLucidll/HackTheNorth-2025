# app.py
import asyncio
import logging
import os
import shutil
import subprocess
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# -----------------------------------------------------------------------------
# Env + Logging
# -----------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

# -----------------------------------------------------------------------------
# App & CORS (single app)
# -----------------------------------------------------------------------------
app = FastAPI(title="InstaShopper API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # lock this down later if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Local storage for uploads/frames
# -----------------------------------------------------------------------------
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Serve frames under /static/<video_id>/frames/frame_XXX.jpg
app.mount("/static", StaticFiles(directory=str(DATA_DIR)), name="static")


# -----------------------------------------------------------------------------
# External helpers (you already have these)
# -----------------------------------------------------------------------------
from .downloader_file import tiktok_to_uploadfile
from .gemini_analyzer import GeminiAnalyzer

# -----------------------------------------------------------------------------
# Models (generic)
# -----------------------------------------------------------------------------
class VideoURL(BaseModel):
    url: str

# -----------------------------------------------------------------------------
# FFmpeg: robust discovery + extraction
# -----------------------------------------------------------------------------
def _find_ffmpeg_binary() -> Optional[str]:
    # 1) Try system ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return "ffmpeg"
    except Exception:
        pass

    # 2) Try imageio_ffmpeg if installed
    try:
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            subprocess.run([exe, "-version"], capture_output=True, check=True)
            return exe
    except Exception:
        pass

    # 3) (Optional) Add any custom paths if you need
    return None


def extract_frames_from_video(input_path: Path, frames_dir: Path, video_id: str) -> List[str]:
    """Extract up to 10 frames at 1 FPS scaled to 720p height."""
    ffmpeg_cmd = _find_ffmpeg_binary()
    if not ffmpeg_cmd:
        raise HTTPException(status_code=500, detail="FFmpeg not found. Install it or add to PATH.")

    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_pattern = str(frames_dir / "frame_%03d.jpg")

    cmd = [
        ffmpeg_cmd,
        "-i",
        str(input_path),
        "-vf",
        "fps=1,scale=720:-1",
        "-frames:v",
        "10",
        "-y",
        frame_pattern,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {e.stderr}")

    frame_files = sorted(frames_dir.glob("frame_*.jpg"))
    return [f"/static/{video_id}/frames/{f.name}" for f in frame_files]

# -----------------------------------------------------------------------------
# Meta endpoints
# -----------------------------------------------------------------------------
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}

@app.get("/api", tags=["meta"])
def api_status():
    return {
        "message": "InstaShopper API is running!",
        "endpoints": ["/docs", "/ingest_tiktok", "/analyze-video", "/analyze-tiktok", "/search"],
    }

@app.get("/", tags=["meta"])
def root():
    return {"message": "Hello from FastAPI!"}

# -----------------------------------------------------------------------------
# TikTok ingestion (only for your own/authorized media)
# -----------------------------------------------------------------------------
TARGET_ENDPOINT = os.getenv("FORWARD_UPLOAD_TARGET", "https://example.com/upload")

@app.post("/ingest_tiktok")
async def ingest_tiktok(url: str):
    try:
        upload = tiktok_to_uploadfile(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")

    async with httpx.AsyncClient(timeout=120) as client:
        files = {"file": (upload.filename, upload.file, upload.content_type)}
        r = await client.post(TARGET_ENDPOINT, files=files)
        r.raise_for_status()
        is_json = r.headers.get("content-type", "").startswith("application/json")
        return {"status": "ok", "forward_resp": r.json() if is_json else r.text}

# -----------------------------------------------------------------------------
# Video analysis
# -----------------------------------------------------------------------------
@app.post("/analyze-tiktok")
async def analyze_tiktok(video_data: VideoURL):
    """Download TikTok video and analyze with Gemini."""
    video_id = str(uuid.uuid4())
    video_dir = DATA_DIR / video_id
    frames_dir = video_dir / "frames"
    video_dir.mkdir(exist_ok=True)
    frames_dir.mkdir(exist_ok=True)

    try:
        upload_file = tiktok_to_uploadfile(video_data.url)
        input_path = video_dir / "input.mp4"
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(upload_file.file, buffer)

        frame_urls = extract_frames_from_video(input_path, frames_dir, video_id)
        analyzer = GeminiAnalyzer()
        analysis_results = analyzer.analyze_all_frames(frames_dir)

        return {
            "video_id": video_id,
            "status": "success",
            "source_url": video_data.url,
            "frames": frame_urls,
            "frame_count": len(frame_urls),
            "analysis": analysis_results,
        }
    except Exception as e:
        if video_dir.exists():
            shutil.rmtree(video_dir)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e}")

@app.post("/analyze-video")
async def analyze_video(file: UploadFile = File(...)):
    """Upload video → extract frames → Gemini → structured items."""
    video_id = str(uuid.uuid4())
    video_dir = DATA_DIR / video_id
    frames_dir = video_dir / "frames"
    video_dir.mkdir(exist_ok=True)
    frames_dir.mkdir(exist_ok=True)

    input_path = video_dir / "input.mp4"
    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save video: {e}")

    try:
        logger.info(f"Extracting frames for video {video_id}")
        frame_urls = extract_frames_from_video(input_path, frames_dir, video_id)
        logger.info(f"Extracted {len(frame_urls)} frames")

        analyzer = GeminiAnalyzer()
        logger.info("Starting Gemini analysis")
        analysis_results = analyzer.analyze_all_frames(frames_dir)
        logger.info("Analysis completed")

        return {
            "video_id": video_id,
            "status": "success",
            "frames": frame_urls,
            "frame_count": len(frame_urls),
            "analysis": analysis_results,
        }

    except Exception as e:
        logger.error(f"Error in analyze_video: {str(e)}")
        logger.error(traceback.format_exc())
        if video_dir.exists():
            shutil.rmtree(video_dir)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

# -----------------------------------------------------------------------------
# Shopify Storefront (Pydantic v1)
# -----------------------------------------------------------------------------
class Settings(BaseSettings):
    shopify_store_domain: str = Field(..., alias="SHOPIFY_STORE_DOMAIN")
    shopify_storefront_token: str = Field(..., alias="SHOPIFY_STOREFRONT_TOKEN")
    shopify_api_version: str = Field("2024-07", alias="SHOPIFY_API_VERSION")
    request_timeout_seconds: int = Field(20, alias="HTTP_TIMEOUT_SECONDS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",   # <- add this line
    )



settings = Settings()

def normalize_domain(domain: Optional[str]) -> str:
    d = (domain or "").strip()
    if d.startswith("http://"):
        d = d[len("http://"):]
    if d.startswith("https://"):
        d = d[len("https://"):]
    return d.strip().strip("/")

def ensure_config_ok() -> str:
    d = normalize_domain(settings.shopify_store_domain)
    if not d or "." not in d:
        raise RuntimeError("Invalid SHOPIFY_STORE_DOMAIN. Expected 'myshop.myshopify.com' (no protocol/no slash).")
    if not settings.shopify_storefront_token:
        raise RuntimeError("Missing SHOPIFY_STOREFRONT_TOKEN.")
    return d

class Money(BaseModel):
    amount: float
    currencyCode: str

class Variant(BaseModel):
    id: str
    title: str
    availableForSale: bool
    selectedOptions: List[Dict[str, str]] = []
    price: Money

class ProductResult(BaseModel):
    id: str
    title: str
    handle: str
    url: Optional[str] = None
    vendor: Optional[str] = None
    productType: Optional[str] = None
    price: Optional[Money] = None
    image: Optional[str] = None
    imageAlt: Optional[str] = None
    variants: List[Variant] = []

class SearchResponse(BaseModel):
    store: str
    query: str
    count: int
    results: List[ProductResult]

class MatchedItem(BaseModel):
    item_index: int
    item: Dict[str, Any]
    query: str
    results: List[ProductResult]

class MatchResponse(BaseModel):
    store: str
    items: List[MatchedItem]

class MatchRequest(BaseModel):
    analysis: Dict[str, Any]     # the whole /analyze-video JSON
    store: Optional[str] = None
    limit_per_item: int = 5
    max_items: int = 6
    price_cap: Optional[float] = None

class CreateCartLine(BaseModel):
    variantId: str
    quantity: int = 1

class CreateCartRequest(BaseModel):
    lines: List[CreateCartLine]
    attributes: Dict[str, str] = {}
    store: Optional[str] = None

class CreateCartResponse(BaseModel):
    cartId: str
    checkoutUrl: str

SEARCH_QUERY = """
query ProductSearch($q: String!, $num: Int!) {
  products(first: $num, query: $q, sortKey: RELEVANCE) {
    edges {
      node {
        id
        title
        vendor
        productType
        handle
        onlineStoreUrl
        tags
        featuredImage { url altText }
        images(first: 1) { edges { node { url altText } } }
        variants(first: 10) {
          edges {
            node {
              id
              title
              availableForSale
              selectedOptions { name value }
              price { amount currencyCode }
            }
          }
        }
      }
    }
  }
}
"""

CART_CREATE = """
mutation CartCreate($lines: [CartLineInput!], $attributes: [AttributeInput!]) {
  cartCreate(input: { lines: $lines, attributes: $attributes }) {
    cart {
      id
      checkoutUrl
      lines(first: 10) {
        edges {
          node {
            id
            quantity
            merchandise { ... on ProductVariant { id title } }
          }
        }
      }
    }
    userErrors { field message }
  }
}
"""

async def shopify_graphql(query: str, variables: Dict[str, Any], store_domain: Optional[str] = None) -> Dict[str, Any]:
    domain = normalize_domain(store_domain) if store_domain else ensure_config_ok()
    token = settings.shopify_storefront_token.strip()
    if not token:
        raise HTTPException(status_code=500, detail="Missing SHOPIFY_STOREFRONT_TOKEN.")
    url = f"https://{domain}/api/{settings.shopify_api_version}/graphql.json"
    headers = {"Content-Type": "application/json", "X-Shopify-Storefront-Access-Token": token}

    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        err_payload = None
        for attempt in range(3):
            resp = await client.post(url, headers=headers, json={"query": query, "variables": variables})
            if resp.status_code != 429:
                if resp.status_code != 200:
                    try:
                        err_payload = resp.json()
                    except Exception:
                        err_payload = resp.text
                    raise HTTPException(status_code=502, detail={"message": "Shopify request failed", "error": err_payload})
                data = resp.json()
                if "errors" in data:
                    raise HTTPException(status_code=502, detail={"message": "Shopify GraphQL error", "error": data["errors"]})
                return data.get("data", {})
            await asyncio.sleep(0.5 * (attempt + 1))

        raise HTTPException(status_code=429, detail="Shopify rate limit exceeded, please retry.")

def _parse_products(domain: str, edges: List[Dict[str, Any]]) -> List[ProductResult]:
    results: List[ProductResult] = []
    for edge in edges:
        node = (edge or {}).get("node", {})
        variant_edges = (node.get("variants") or {}).get("edges", []) or []
        featured = node.get("featuredImage") or {}
        img_edges = (node.get("images") or {}).get("edges", []) or []
        img_node = featured or (img_edges[0]["node"] if img_edges else {})

        # price for card (first variant)
        price_node = None
        if variant_edges:
            v = variant_edges[0].get("node", {}) or {}
            price_node = v.get("price")

        # URL: prefer onlineStoreUrl, else fallback /products/{handle}
        fallback_url = f"https://{domain}/products/{node.get('handle','')}" if node.get("handle") else None
        url = node.get("onlineStoreUrl") or fallback_url

        variants: List[Variant] = []
        for ve in variant_edges:
            n = ve.get("node", {}) or {}
            if not n.get("id") or not n.get("price"):
                continue
            variants.append(
                Variant(
                    id=n["id"],
                    title=n.get("title", ""),
                    availableForSale=bool(n.get("availableForSale")),
                    selectedOptions=[{"name": so["name"], "value": so["value"]} for so in (n.get("selectedOptions") or [])],
                    price=Money(amount=float(n["price"]["amount"]), currencyCode=n["price"]["currencyCode"]),
                )
            )

        results.append(
            ProductResult(
                id=node.get("id", ""),
                title=node.get("title", ""),
                handle=node.get("handle", ""),
                url=url,
                vendor=node.get("vendor"),
                productType=node.get("productType"),
                price=(Money(amount=float(price_node["amount"]), currencyCode=price_node["currencyCode"]) if price_node else None),
                image=img_node.get("url"),
                imageAlt=img_node.get("altText"),
                variants=variants,
            )
        )
    return results

async def search_shopify_products(query: str, limit: int = 5, store_domain: Optional[str] = None) -> List[ProductResult]:
    domain = normalize_domain(store_domain) if store_domain else ensure_config_ok()
    variables = {"q": query, "num": max(1, min(limit, 50))}
    data = await shopify_graphql(SEARCH_QUERY, variables, store_domain=domain)
    edges = (data.get("products") or {}).get("edges", []) or []
    return _parse_products(domain, edges)

def build_shopify_query_from_item(item: Dict[str, Any]) -> str:
    # item keys: type, color, pattern, material, brand_text, description
    tokens: List[str] = []

    t = (item.get("type") or "").strip()
    if t:
        tokens.append(t)

    b = (item.get("brand_text") or "").strip().strip('"')
    if b:
        tokens.append(f'"{b}"')  # quote multi-word brands

    for k in ("color", "pattern", "material"):
        v = (item.get(k) or "").strip()
        if v:
            tokens.append(v)

    desc = (item.get("description") or "").lower()
    words = [w for w in desc.split() if w.isalpha()]
    tokens += words[:3]  # a few helper words

    # dedupe + lowercase
    seen, clean = set(), []
    for tok in tokens:
        tok = tok.lower()
        if tok and tok not in seen:
            seen.add(tok)
            clean.append(tok)

    return " ".join(clean) or (t or "clothing")

# -------------------------
# 1) Raw search endpoint
# -------------------------
@app.get("/search", response_model=SearchResponse, tags=["shopify"])
async def search(
    q: str = Query(..., description='Search, e.g., black nike "high-top" leather sneaker'),
    limit: int = Query(5, ge=1, le=50),
    store: Optional[str] = Query(None, description="Override store domain, e.g., 'myshop.myshopify.com'"),
):
    results = await search_shopify_products(query=q, limit=limit, store_domain=store)
    return SearchResponse(
        store=normalize_domain(store) if store else normalize_domain(settings.shopify_store_domain),
        query=q,
        count=len(results),
        results=results,
    )

# ---------------------------------------------------------
# 2) Search directly from /analyze-video output
# ---------------------------------------------------------
@app.post("/shopify/search-from-analysis", response_model=MatchResponse, tags=["shopify"])
async def search_from_analysis(payload: MatchRequest = Body(...)):
    store_domain = payload.store
    consolidated = (payload.analysis.get("analysis") or {}).get("consolidated_products", [])
    consolidated = consolidated[: payload.max_items]

    matched: List[MatchedItem] = []
    for idx, item in enumerate(consolidated):
        q = build_shopify_query_from_item(item)
        results = await search_shopify_products(q, limit=payload.limit_per_item, store_domain=store_domain)

        # If empty, relax by dropping brand_text
        if not results and item.get("brand_text"):
            relaxed = dict(item)
            relaxed["brand_text"] = None
            rq = build_shopify_query_from_item(relaxed)
            results = await search_shopify_products(rq, limit=payload.limit_per_item, store_domain=store_domain)
            if results:
                q = rq

        if payload.price_cap is not None:
            results = [r for r in results if r.price and r.price.amount <= payload.price_cap]

        matched.append(MatchedItem(item_index=idx, item=item, query=q, results=results))

    return MatchResponse(
        store=normalize_domain(store_domain) if store_domain else normalize_domain(settings.shopify_store_domain),
        items=matched,
    )

# -------------------------------------------
# 3) Create a cart + return checkout URL
# -------------------------------------------
@app.post("/shopify/create-cart", response_model=CreateCartResponse, tags=["shopify"])
async def create_cart(payload: CreateCartRequest = Body(...)):
    store_domain = payload.store
    lines = [{"quantity": l.quantity, "merchandiseId": l.variantId} for l in payload.lines]
    attributes = [{"key": k, "value": v} for k, v in (payload.attributes or {}).items()]
    data = await shopify_graphql(CART_CREATE, {"lines": lines, "attributes": attributes}, store_domain=store_domain)

    cart = (data.get("cartCreate") or {}).get("cart")
    errors = (data.get("cartCreate") or {}).get("userErrors") or []
    if errors:
        raise HTTPException(status_code=400, detail={"message": "Cart create error", "errors": errors})
    if not cart or not cart.get("checkoutUrl"):
        raise HTTPException(status_code=502, detail="No checkoutUrl returned by Shopify.")
    return CreateCartResponse(cartId=cart["id"], checkoutUrl=cart["checkoutUrl"])
