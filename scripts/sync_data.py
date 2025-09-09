import os
import json
import numpy as np
import faiss
import mysql.connector
from openai import OpenAI
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()
BATCH_SIZE = 10
EMBEDDING_DIMENSION = 1024
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
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME")
        )
        return conn
    except mysql.connector.Error as e:
        print(f"Error connecting to MySQL Database: {e}")
        exit(1)

def main():
    """Main function to run the data synchronization and vectorization."""
    print("--- Starting Data Synchronization ---")

    faiss_index = None
    id_mapping = {}
    max_id = 0

    # Step 1: Try to load existing data to determine the update mode
    try:
        print("Checking for existing data...")
        faiss_index = faiss.read_index(FAISS_INDEX_PATH)
        with open(ID_MAPPING_PATH, 'r') as f:
            # JSON keys are strings, convert them back to int for mapping
            id_mapping = {int(k): v for k, v in json.load(f).items()}

        if id_mapping:
            max_id = max(id_mapping.values())

        print(f"Found existing data. Last thread ID is {max_id}. Starting INCREMENTAL update.")

    except FileNotFoundError:
        print("No existing data found. Starting FULL rebuild.")
        faiss_index = faiss.IndexFlatL2(EMBEDDING_DIMENSION)
        id_mapping = {}

    # Step 2: Fetch new threads from the database
    conn = get_db_connection()
    cursor = conn.cursor()

    base_query = """
    SELECT t.id, t.title, c.markdown
    FROM threads AS t
    JOIN contents AS c ON t.id = c.contentable_id
    WHERE t.deleted_at IS NULL AND t.banned_at IS NULL AND c.contentable_type = 'App\\\\Thread'
    """

    if max_id > 0:
        query = base_query + f" AND t.id > {max_id}"
    else:
        query = base_query

    print(f"Executing query to fetch new threads (ID > {max_id})...")
    cursor.execute(query)
    new_threads = cursor.fetchall()
    cursor.close()
    conn.close()

    if not new_threads:
        print("No new threads found. Knowledge base is up to date.")
        print("--- Synchronization Process Finished Successfully! ---")
        return

    print(f"Found {len(new_threads)} new threads to process.")

    # Step 3: Prepare texts and create embeddings for new threads
    texts_to_embed = []
    new_thread_ids = []
    max_len = 8192

    for id, title, markdown in new_threads:
        text = f"标题：{title}\n内容：{markdown}"
        if len(text) > max_len:
            text = text[:max_len]
        if not text.strip():
            continue
        texts_to_embed.append(text)
        new_thread_ids.append(id)

    all_new_embeddings = []
    print(f"Starting to create embeddings for {len(texts_to_embed)} valid new threads...")

    for i in range(0, len(texts_to_embed), BATCH_SIZE):
        batch_texts = texts_to_embed[i:i + BATCH_SIZE]
        try:
            response = client.embeddings.create(
                model="text-embedding-v4",
                input=batch_texts,
                dimensions=EMBEDDING_DIMENSION
            )
            batch_embeddings = [item.embedding for item in response.data]
            all_new_embeddings.extend(batch_embeddings)
            print(f"  - Processed batch {i//BATCH_SIZE + 1}/{-(-len(texts_to_embed)//BATCH_SIZE)}")
        except Exception as e:
            print(f"An error occurred during embedding creation for batch {i}: {e}")
            continue

    if not all_new_embeddings:
        print("No new embeddings were created. Exiting.")
        return

    # Step 4: Add new vectors to index and update mapping
    start_index = faiss_index.ntotal
    new_embeddings_np = np.array(all_new_embeddings, dtype='float32')
    faiss_index.add(new_embeddings_np)

    for i, thread_id in enumerate(new_thread_ids):
        id_mapping[start_index + i] = thread_id

    # Step 5: Save updated data to disk
    os.makedirs(os.path.dirname(FAISS_INDEX_PATH), exist_ok=True)
    print(f"Saving FAISS index with a total of {faiss_index.ntotal} vectors...")
    faiss.write_index(faiss_index, FAISS_INDEX_PATH)

    print("Saving ID mapping...")
    with open(ID_MAPPING_PATH, 'w') as f:
        json.dump(id_mapping, f)

    print("--- Synchronization Process Finished Successfully! ---")

if __name__ == "__main__":
    main()