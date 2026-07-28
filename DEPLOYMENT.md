# Deployment Guide

## Deploying to Render

### Prerequisites
- GitHub account
- Render account (free tier available at https://render.com)
- API keys for OpenAI and Backblaze B2

### Step-by-Step Deployment

#### 1. Prepare Your Repository

```bash
# Initialize git repository (if not already done)
git init

# Add all files
git add .

# Commit
git commit -m "Initial commit: Media Transcription Service"

# Create GitHub repository and push
git remote add origin https://github.com/yourusername/media-transcription-service.git
git branch -M main
git push -u origin main
```

#### 2. Deploy on Render

1. **Sign in to Render**
   - Go to https://dashboard.render.com
   - Sign in or create an account

2. **Create New Blueprint**
   - Click "New +" button
   - Select "Blueprint"
   - Connect your GitHub account if not already connected
   - Select your repository

3. **Configure Service**
   - Render will automatically detect `render.yaml`
   - Review the configuration:
     - Service Type: Web Service
     - Environment: Docker
     - Region: Oregon (or your preferred region)
     - Plan: Starter (or your preferred plan)

4. **Set Environment Variables**
   - In the Render dashboard, go to your service
   - Navigate to "Environment" tab
   - Add the following variables:
     ```
     API_KEY=your-secret-api-key
     OPENAI_API_KEY=sk-your-openai-api-key
     B2_ARCHIVE_KEY_ID=your-server-side-b2-key-id
     B2_ARCHIVE_APPLICATION_KEY=your-server-side-b2-application-key
     B2_ARCHIVE_BUCKET=your-media-bucket
     B2_VIDEO_PREFIX=videos
     B2_THUMBNAIL_PREFIX=thumbnails
     THUMBNAIL_AT_SECONDS=3
     ```

5. **Deploy**
   - Click "Create Blueprint" or "Deploy"
   - Render will:
     - Build your Docker image
     - Deploy the container
     - Assign a public URL

6. **Verify Deployment**
   - Once deployed, your service URL will be: `https://your-service-name.onrender.com`
   - Test the health endpoint: `https://your-service-name.onrender.com/health`
   - Should return: `{"status": "healthy"}`

#### 3. Test Your Deployment

```bash
# Test health endpoint
curl https://your-service-name.onrender.com/health

# Submit a transcription job
curl -X POST https://your-service-name.onrender.com/transcribe \
  -H "X-API-KEY: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "b2_bucket": "your-bucket",
    "b2_file_path": "path/to/media.mp4",
    "callback_url": "https://your-webhook.com/callback"
  }'

# Check job status
curl https://your-service-name.onrender.com/jobs/{job_id} \
  -H "X-API-KEY: your-api-key"
```

### Automatic Deployments

Render automatically deploys when you push to your main branch:

```bash
# Make changes to your code
git add .
git commit -m "Update feature"
git push origin main

# Render will automatically rebuild and redeploy
```

### Monitoring

1. **Logs**
   - View logs in Render dashboard under "Logs" tab
   - Real-time log streaming available

2. **Metrics**
   - CPU and memory usage visible in dashboard
   - Request metrics and response times

3. **Health Checks**
   - Render automatically monitors `/health` endpoint
   - Service restarts if health checks fail

### Scaling

To handle more traffic:

1. **Vertical Scaling**
   - Upgrade to a larger plan in Render dashboard
   - More CPU and memory per instance

2. **Horizontal Scaling**
   - Note: Current implementation uses in-memory job storage
   - For multiple instances, consider adding Redis or database for job state

### Troubleshooting

**Build Fails**
- Check Dockerfile syntax
- Verify all dependencies in requirements.txt
- Review build logs in Render dashboard

**Service Crashes**
- Check environment variables are set correctly
- Review application logs
- Verify ffmpeg is installed (included in Dockerfile)

**Slow Performance**
- Consider upgrading Render plan
- Check OpenAI API rate limits
- Monitor B2 download speeds

**Out of Memory**
- Large media files may require more memory
- Upgrade to plan with more RAM
- Consider implementing file size limits

### Cost Optimization

1. **Free Tier**
   - Render offers free tier with limitations
   - Service spins down after inactivity
   - First request after spin-down may be slow

2. **Paid Plans**
   - Starter plan keeps service always running
   - Better for production use
   - Predictable performance

3. **Resource Management**
   - Service automatically cleans up temp files
   - No persistent storage needed
   - Minimal disk usage

### Security Best Practices

1. **API Keys**
   - Never commit `.env` file
   - Use Render's environment variables
   - Rotate keys regularly

2. **HTTPS**
   - Render provides free SSL certificates
   - All traffic encrypted by default

3. **Rate Limiting**
   - Consider adding rate limiting middleware
   - Protect against abuse

### Support

- Render Documentation: https://render.com/docs
- Render Community: https://community.render.com
- Service Issues: Check logs and health endpoint first
