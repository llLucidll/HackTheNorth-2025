# InstaShopper — From Reel to Real

Turn short-form videos into instant shopping.  
Paste a TikTok/Instagram clip → extract a few frames → detect outfit items → find the closest products from Shopify → one-tap checkout. Optional voice refinements via VAPI.

---

## ✨ Why this matters

People see outfits they love on Reels/TikTok but **can’t find the exact items**. InstaShopper bridges the gap with a multimodal AI → commerce loop.

---

## 🧭 How it works (MVP pipeline)

1) **Video Ingestion**  
   - Users upload a short clip or paste a link.  
   - **FFmpeg** extracts ~5–10 frames (≈1 fps).

2) **Visual Understanding**  
   - **Gemini Vision** analyzes each frame and emits structured attributes:  
     *type (sneaker/hoodie/bag), color/pattern, material, brand-like logo/text.*

3) **Object Detection & Crops (optional but improves quality)**  
   - Run a light **detector** (e.g., YOLO/OVD) to localize items and crop product patches.  
   - Crops and/or Gemini attributes feed the matcher.

4) **Product Matching**  
   - Convert item descriptions (and/or crop images) into **embeddings** with **Cohere** (or CLIP).  
   - Store vectors in **Postgres + pgvector** and run **nearest-neighbor** search.  
   - **Shopify Storefront API** returns live prices, images, and checkout.

5) **Shopping Experience**  
   - Show top matches as product cards (image, title, price, **Buy**).  
   - **VAPI** lets users say: “show only hoodies under $80”, “women’s sizes only”.

---

## 🧱 Tech Stack

- **Frontend:** React/Next.js (upload, results grid, voice refinements)  
- **Backend:** FastAPI (Python) on AWS (Lambda/API Gateway or a small EC2)  
- **Media:** FFmpeg (frame sampling, cropping)  
- **Vision/NLP:** Gemini API (Vision + Text), Cohere (embeddings)  
- **Commerce:** Shopify Storefront API (catalog, checkout)  
- **DB/Vector:** Postgres + pgvector  
- **Voice:** VAPI (optional)

---

## 🚀 Quick Start

### 1) Prereqs
- Python 3.10+, Node 18+, FFmpeg installed (`ffmpeg -version`)
- Postgres 14+ with `pgvector` extension
- API keys: `GEMINI_API_KEY`, `COHERE_API_KEY`, `SHOP` (e.g., `mystore.myshopify.com`), `STOREFRONT_ACCESS_TOKEN`

### 2) Backend env (`/backend/.env`)
```bash
GEMINI_API_KEY=***
COHERE_API_KEY=***
SHOP=mystore.myshopify.com
STOREFRONT_ACCESS_TOKEN=***
DATABASE_URL=postgresql://user:pass@localhost:5432/instashop
```
### 3) Start instructions
uvicorn app.app:app --reload --host 0.0.0.0 --port 8000 
