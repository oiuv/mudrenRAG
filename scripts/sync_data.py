
import os
import json
import numpy as np
import faiss
import mysql.connector
from openai import OpenAI
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()
BATCH_SIZE = 10  # Batch size for calling the embedding API (max allowed by DashScope is 10)
EMBEDDING_DIMENSION = 1024  # Dimension of the embeddings
FAISS_INDEX_PATH = "data/threads.index"
ID_MAPPING_PATH = "data/id_mapping.json"

# --- Initialize DashScope Client ---
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
)

def get_db_connection():
    """Establishes connection to the MySQL database."""
    try:
        print("--- Debugging Connection Info ---")
        db_host = os.getenv("DB_HOST")
        db_port = os.getenv("DB_PORT")
        db_user = os.getenv("DB_USER")
        db_name = os.getenv("DB_NAME")
        print(f"  - Host: {db_host}")
        print(f"  - Port: {db_port}")
        print(f"  - User: {db_user}")
        print(f"  - Database: {db_name}")
        print("---------------------------------")

        conn = mysql.connector.connect(
            host=db_host,
            port=db_port,
            user=db_user,
            password=os.getenv("DB_PASSWORD"),
            database=db_name
        )
        
        # Verify the current database
        test_cursor = conn.cursor()
        test_cursor.execute("SELECT DATABASE();")
        current_db = test_cursor.fetchone()
        print(f"Successfully connected. Current database in use: '{current_db[0] if current_db else 'NOT FOUND'}'")
        test_cursor.close()
        
        return conn
    except mysql.connector.Error as e:
        print(f"Error connecting to MySQL Database: {e}")
        exit(1)

def fetch_threads():
    """Fetches all threads from the database."""
    print("Connecting to the database...")
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # The value is 'App\Thread', which needs to be escaped as 'App\\Thread' in a Python string.
    query = """
    SELECT t.id, t.title, c.markdown
    FROM threads AS t
    JOIN contents AS c ON t.id = c.contentable_id
    WHERE t.deleted_at IS NULL 
      AND t.banned_at IS NULL
      AND c.contentable_type = 'App\\\\Thread';
    """
    
    print("Executing query to fetch threads...")
    cursor.execute(query)
    threads = cursor.fetchall()
    
    cursor.close()
    conn.close()
    print(f"Found {len(threads)} threads to process.")
    return threads


def create_embeddings(threads):
    """Creates embeddings for all threads using DashScope API."""
    if not threads:
        print("No threads to embed.")
        return np.array([]), {}

    texts_to_embed = []
    thread_ids = []
    max_len = 8192

    print("Preparing and validating texts for embedding...")
    for id, title, markdown in threads:
        # Combine title and content
        text = f"标题：{title}\n内容：{markdown}"
        
        # Truncate if text is too long
        if len(text) > max_len:
            print(f"  - [WARN] Thread ID {id} is too long ({len(text)} chars). Truncating to {max_len} chars.")
            text = text[:max_len]
        
        # Skip if text is empty
        if not text.strip():
            print(f"  - [WARN] Thread ID {id} is empty. Skipping.")
            continue
        
        texts_to_embed.append(text)
        thread_ids.append(id)

    all_embeddings = []
    print(f"Starting to create embeddings for {len(texts_to_embed)} valid threads in batches of {BATCH_SIZE}...")

    for i in range(0, len(texts_to_embed), BATCH_SIZE):
        batch_texts = texts_to_embed[i:i + BATCH_SIZE]
        
        try:
            response = client.embeddings.create(
                model="text-embedding-v4",
                input=batch_texts,
                dimensions=EMBEDDING_DIMENSION
            )
            batch_embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(batch_embeddings)
            print(f"  - Processed batch {i//BATCH_SIZE + 1}/{-(-len(texts_to_embed)//BATCH_SIZE)} ({len(all_embeddings)} embeddings total)")
        except Exception as e:
            print(f"An error occurred during embedding creation for batch {i}: {e}")
            # Optional: decide if you want to stop or continue
            continue
            
    # Create a mapping from FAISS index (0, 1, 2...) to thread_id
    id_mapping = {i: thread_ids[i] for i in range(len(all_embeddings))}
    
    return np.array(all_embeddings, dtype='float32'), id_mapping

def build_and_save_index(embeddings, id_mapping):
    """Builds a FAISS index and saves it to disk."""
    if embeddings.shape[0] == 0:
        print("No embeddings were created. Skipping index creation.")
        return

    print(f"Building FAISS index with {embeddings.shape[0]} vectors of dimension {embeddings.shape[1]}...")
    index = faiss.IndexFlatL2(EMBEDDING_DIMENSION)
    index.add(embeddings)
    
    # Create data directory if it doesn't exist
    os.makedirs(os.path.dirname(FAISS_INDEX_PATH), exist_ok=True)
    
    print(f"Saving FAISS index to {FAISS_INDEX_PATH}")
    faiss.write_index(index, FAISS_INDEX_PATH)
    
    print(f"Saving ID mapping to {ID_MAPPING_PATH}")
    with open(ID_MAPPING_PATH, 'w') as f:
        json.dump(id_mapping, f)

def main():
    """Main function to run the data synchronization and vectorization."""
    print("--- Starting Data Synchronization and Vectorization ---")
    threads = fetch_threads()
    embeddings, id_mapping = create_embeddings(threads)
    build_and_save_index(embeddings, id_mapping)
    print("--- Synchronization Process Finished Successfully! ---")

if __name__ == "__main__":
    main()
