"""Simple API test script"""
import requests
import sys
import os
from dotenv import load_dotenv

load_dotenv()

# Configuration
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY", "your-secret-api-key")

def test_health():
    """Test health endpoint"""
    print("Testing health endpoint...")
    response = requests.get(f"{BASE_URL}/health")
    print(f"Status: {response.status_code}")
    print(f"Response: {response.json()}")
    return response.status_code == 200

def test_auth():
    """Test authentication"""
    print("\nTesting authentication...")
    
    # Test without API key
    response = requests.post(f"{BASE_URL}/transcribe", json={})
    print(f"Without API key - Status: {response.status_code} (expected 422)")
    
    # Test with invalid API key
    response = requests.post(
        f"{BASE_URL}/transcribe",
        headers={"X-API-KEY": "invalid-key"},
        json={
            "b2_bucket": "test",
            "b2_file_path": "test.mp4",
            "callback_url": "https://example.com/callback"
        }
    )
    print(f"Invalid API key - Status: {response.status_code} (expected 401)")
    
    return True

def test_job_creation():
    """Test job creation"""
    print("\nTesting job creation...")
    
    response = requests.post(
        f"{BASE_URL}/transcribe",
        headers={"X-API-KEY": API_KEY},
        json={
            "b2_bucket": "test-bucket",
            "b2_file_path": "test/sample.mp4",
            "callback_url": "https://webhook.site/unique-id"
        }
    )
    
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"Job ID: {data.get('job_id')}")
        print(f"Status: {data.get('status')}")
        return data.get('job_id')
    else:
        print(f"Error: {response.text}")
        return None

def test_job_status(job_id):
    """Test job status endpoint"""
    if not job_id:
        print("\nSkipping job status test (no job ID)")
        return
    
    print(f"\nTesting job status for {job_id}...")
    
    response = requests.get(
        f"{BASE_URL}/jobs/{job_id}",
        headers={"X-API-KEY": API_KEY}
    )
    
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"Job Status: {data.get('status')}")
        print(f"Progress: {data.get('progress')}%")
        if data.get('error'):
            print(f"Error: {data.get('error')}")

def main():
    """Run all tests"""
    print(f"Testing API at: {BASE_URL}")
    print(f"Using API Key: {API_KEY[:10]}...")
    print("=" * 50)
    
    try:
        # Test health
        if not test_health():
            print("\n❌ Health check failed!")
            sys.exit(1)
        
        # Test auth
        test_auth()
        
        # Test job creation
        job_id = test_job_creation()
        
        # Test job status
        test_job_status(job_id)
        
        print("\n" + "=" * 50)
        print("✅ All tests completed!")
        
    except requests.exceptions.ConnectionError:
        print(f"\n❌ Could not connect to {BASE_URL}")
        print("Make sure the service is running!")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
