"""
Batch Transcription Script
Walks through all folders in a B2 bucket and submits transcription jobs for mp3 and wav files.
"""
from b2sdk.v2 import B2Api, InMemoryAccountInfo
import os
import requests
import time
from dotenv import load_dotenv

load_dotenv()

# Configuration
B2_KEY_ID = os.getenv("B2_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY")
API_KEY = os.getenv("API_KEY", "your-secret-api-key")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
BUCKET_NAME = "TCPTRANSFER"
CALLBACK_URL = "https://thecollaborativeprocess.app.n8n.cloud/webhook-test/67becaf6-74cd-4577-95f7-b362c5bf89ef"
UPLOAD_TRANSCRIPT_TO_B2 = True  # Set to True to upload transcripts to B2, False to skip

# Supported audio extensions
SUPPORTED_EXTENSIONS = [".mp3", ".wav"]

# Initialize B2 API
info = InMemoryAccountInfo()
api = B2Api(info)
api.authorize_account("production", B2_KEY_ID, B2_APPLICATION_KEY)

def submit_transcription_job(bucket_name: str, file_path: str, callback_url: str, upload_to_b2: bool = False):
    """Submit a transcription job to the API"""
    url = f"{BASE_URL}/transcribe"
    headers = {
        "X-API-KEY": API_KEY,
        "Content-Type": "application/json",
        "X-Upload-Transcript": str(upload_to_b2).lower()  # Convert boolean to "true" or "false"
    }
    payload = {
        "b2_bucket": bucket_name,
        "b2_file_path": file_path,
        "callback_url": callback_url
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        print(f"✓ Submitted: {file_path}")
        print(f"  Job ID: {result['job_id']}, Status: {result['status']}")
        return result
    except requests.exceptions.RequestException as e:
        print(f"✗ Failed to submit: {file_path}")
        print(f"  Error: {e}")
        return None

def main():
    """Main function to process all audio files in the bucket"""
    print(f"Scanning bucket: {BUCKET_NAME}")
    print(f"Looking for files with extensions: {', '.join(SUPPORTED_EXTENSIONS)}")
    print(f"Callback URL: {CALLBACK_URL}")
    print(f"Upload transcripts to B2: {UPLOAD_TRANSCRIPT_TO_B2}")
    print("-" * 80)
    
    bucket = api.get_bucket_by_name(BUCKET_NAME)
    
    submitted_count = 0
    skipped_count = 0
    failed_count = 0
    
    # Walk through all files in the bucket
    for file_info, _ in bucket.ls(recursive=True):
        file_name = file_info.file_name
        file_extension = os.path.splitext(file_name)[1].lower()
        
        # Check if file has supported extension
        if file_extension in SUPPORTED_EXTENSIONS:
            print(f"\nProcessing: {file_name}")
            result = submit_transcription_job(BUCKET_NAME, file_name, CALLBACK_URL, UPLOAD_TRANSCRIPT_TO_B2)
            
            if result:
                submitted_count += 1
                # Small delay to avoid overwhelming the API
                time.sleep(0.5)
            else:
                failed_count += 1
        else:
            skipped_count += 1
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total files submitted: {submitted_count}")
    print(f"Total files skipped: {skipped_count}")
    print(f"Total files failed: {failed_count}")
    print(f"\nYou can check job status at: {BASE_URL}/jobs/{{job_id}}")

if __name__ == "__main__":
    main()
