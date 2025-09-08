
import os
import json
import numpy as np
import faiss
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from openai import OpenAI
from dotenv import load_dotenv

from .models import RetrievalRequest, RetrievalResponse, Record, ErrorResponse

# --- Configuration & Initialization ---
load_dotenv()

# FastAPI App
app = FastAPI(
    title="MyRAG Retrieval Service for Dify",
    description="A custom RAG knowledge base API for forum posts.",
    version="1.0.0"
)

# --- Global Objects ---
# These objects are loaded once at startup.
faiss_index = None
id_mapping = None
client = None

# --- Environment Variables ---
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
EMBEDDING_DIMENSION = 1024
FAISS_INDEX_PATH = "data/threads.index"
ID_MAPPING_PATH = "data/id_mapping.json"

@app.on_event("startup")
def startup_event():
    """Load resources on server startup."""
    global faiss_index, id_mapping, client

    print("--- Server is starting up... ---")

    # Load FAISS index
    try:
        print(f"Loading FAISS index from {FAISS_INDEX_PATH}...")
        faiss_index = faiss.read_index(FAISS_INDEX_PATH)
        print(f"Index loaded successfully. Contains {faiss_index.ntotal} vectors.")
    except Exception as e:
        print(f"[ERROR] Could not load FAISS index: {e}")
        # This is a critical error, you might want the app to fail startup
        # For now, we'll let it run but it will fail on requests.

    # Load ID mapping
    try:
        print(f"Loading ID mapping from {ID_MAPPING_PATH}...")
        with open(ID_MAPPING_PATH, 'r') as f:
            id_mapping = json.load(f)
        print("ID mapping loaded successfully.")
    except Exception as e:
        print(f"[ERROR] Could not load ID mapping: {e}")

    # Initialize DashScope Client
    print("Initializing DashScope client...")
    client = OpenAI(
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    print("DashScope client initialized.")
    print("--- Server startup complete! ---")


@app.post("/retrieval", 
          response_model=RetrievalResponse, 
          responses={403: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def retrieval(request: RetrievalRequest, authorization: str = Header(None)):
    """Main endpoint for Dify to retrieve knowledge."""
    # 1. Check if resources are loaded
    if not faiss_index or not id_mapping or not client:
        raise HTTPException(status_code=500, detail={"error_code": 5001, "error_msg": "Server resources not initialized."})

    # 2. Authentication
    if not authorization or authorization != f"Bearer {DIFY_API_KEY}":
        raise HTTPException(status_code=403, detail={"error_code": 1002, "error_msg": "Invalid or missing API Key."})

    # 3. Vectorize the user's query
    try:
        response = client.embeddings.create(
            model="text-embedding-v4",
            input=request.query,
            dimensions=EMBEDDING_DIMENSION
        )
        query_vector = response.data[0].embedding
        query_vector_np = np.array([query_vector], dtype='float32')
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error_code": 5002, "error_msg": f"Failed to vectorize query: {e}"})

    # 4. Search in FAISS
    top_k = request.retrieval_setting.top_k
    distances, indices = faiss_index.search(query_vector_np, top_k)

    # 5. Process results and fetch full content
    records = []
    async with httpx.AsyncClient() as http_client:
        for i, dist in zip(indices[0], distances[0]):
            # Convert L2 distance to a 0-1 similarity score (a common approach)
            score = 1.0 / (1.0 + dist)
            
            if score < request.retrieval_setting.score_threshold:
                continue

            thread_id = id_mapping.get(str(i)) # ID mapping keys are strings
            if not thread_id:
                continue

            try:
                # Fetch full content from the external API
                api_url = f"https://api.mud.ren/threads/{thread_id}"
                api_response = await http_client.get(api_url)
                api_response.raise_for_status() # Raise an exception for 4xx/5xx responses
                thread_data = api_response.json()

                record = Record(
                    content=thread_data.get('content', {}).get('markdown', 'No content'),
                    score=round(score, 4),
                    title=thread_data.get('title', 'No Title'),
                    metadata={
                        "thread_id": thread_id,
                        "url": f"https://bbs.mud.ren/threads/{thread_id}",
                        "user_name": thread_data.get('user', {}).get('name', 'Unknown'),
                        "published_at": thread_data.get('published_at', None)
                    }
                )
                records.append(record)

            except httpx.HTTPStatusError as e:
                print(f"[WARN] Failed to fetch thread {thread_id}. Status: {e.response.status_code}")
            except Exception as e:
                print(f"[WARN] An error occurred processing thread {thread_id}: {e}")

    return RetrievalResponse(records=records)
