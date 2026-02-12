# Deployment Checklist

## Pre-Deployment

### 1. Environment Setup
- [ ] Copy `.env.example` to `.env`
- [ ] Set `API_KEY` (generate a secure random key)
- [ ] Set `OPENAI_API_KEY` (from OpenAI dashboard)
- [ ] Set `B2_KEY_ID` (from Backblaze dashboard)
- [ ] Set `B2_APPLICATION_KEY` (from Backblaze dashboard)

### 2. Local Testing
- [ ] Install dependencies: `pip install -r requirements.txt`
- [ ] Install ffmpeg on your system
- [ ] Run service locally: `python main.py`
- [ ] Test health endpoint: `curl http://localhost:8000/health`
- [ ] Run test script: `python test_api.py`
- [ ] Verify all tests pass

### 3. Docker Testing (Optional but Recommended)
- [ ] Build Docker image: `docker build -t media-transcription-service .`
- [ ] Run container: `docker-compose up`
- [ ] Test endpoints with Docker container
- [ ] Verify logs show no errors
- [ ] Stop container: `docker-compose down`

## Git Repository Setup

### 4. Initialize Repository
- [ ] Initialize git: `git init`
- [ ] Verify `.gitignore` is present
- [ ] Add files: `git add .`
- [ ] Commit: `git commit -m "Initial commit"`
- [ ] Create GitHub repository
- [ ] Add remote: `git remote add origin <your-repo-url>`
- [ ] Push: `git push -u origin main`

### 5. Verify Repository
- [ ] Check `.env` is NOT in repository (should be ignored)
- [ ] Verify all source files are present
- [ ] Confirm `Dockerfile` and `render.yaml` are included

## Render Deployment

### 6. Create Render Account
- [ ] Sign up at https://render.com
- [ ] Verify email address
- [ ] Connect GitHub account

### 7. Deploy Service
- [ ] Click "New +" → "Blueprint"
- [ ] Select your repository
- [ ] Verify `render.yaml` is detected
- [ ] Review service configuration
- [ ] Click "Create Blueprint"

### 8. Configure Environment Variables
- [ ] Navigate to service in Render dashboard
- [ ] Go to "Environment" tab
- [ ] Add `API_KEY`
- [ ] Add `OPENAI_API_KEY`
- [ ] Add `B2_KEY_ID`
- [ ] Add `B2_APPLICATION_KEY`
- [ ] Save changes

### 9. Wait for Deployment
- [ ] Monitor build logs
- [ ] Wait for "Live" status
- [ ] Note your service URL

## Post-Deployment Verification

### 10. Test Deployed Service
- [ ] Test health endpoint: `curl https://your-service.onrender.com/health`
- [ ] Run test script against production: `BASE_URL=https://your-service.onrender.com python test_api.py`
- [ ] Verify authentication works
- [ ] Test job creation (will fail at download if test file doesn't exist - this is expected)

### 11. Integration Testing
- [ ] Upload a test media file to Backblaze
- [ ] Submit real transcription job
- [ ] Verify webhook callback is received
- [ ] Check job status endpoint
- [ ] Verify transcript is returned

### 12. Monitoring Setup
- [ ] Bookmark Render dashboard
- [ ] Set up log monitoring
- [ ] Test health check endpoint
- [ ] Verify automatic restarts work

## Production Readiness

### 13. Security Review
- [ ] Verify API key is strong and unique
- [ ] Confirm HTTPS is enabled (automatic on Render)
- [ ] Test invalid API key rejection
- [ ] Verify no sensitive data in logs

### 14. Performance Testing
- [ ] Test with small file (< 1 minute)
- [ ] Test with medium file (5-10 minutes)
- [ ] Test with large file (> 30 minutes)
- [ ] Verify chunking works correctly
- [ ] Check parallel processing

### 15. Error Handling
- [ ] Test with non-existent B2 file
- [ ] Test with invalid callback URL
- [ ] Test with unsupported file format
- [ ] Verify error messages are clear
- [ ] Confirm failed jobs send webhook

### 16. Documentation
- [ ] Update README with production URL
- [ ] Document any custom configuration
- [ ] Share API documentation with team
- [ ] Document webhook payload format

## Ongoing Maintenance

### 17. Monitoring
- [ ] Check logs weekly
- [ ] Monitor error rates
- [ ] Track API usage costs (OpenAI, B2, Render)
- [ ] Review performance metrics

### 18. Updates
- [ ] Keep dependencies updated
- [ ] Monitor for security advisories
- [ ] Test updates in staging first
- [ ] Document changes in git commits

### 19. Scaling Considerations
- [ ] Monitor request volume
- [ ] Check processing times
- [ ] Consider upgrading Render plan if needed
- [ ] Evaluate need for job queue (Redis/database)

### 20. Backup & Recovery
- [ ] Document environment variables securely
- [ ] Keep backup of configuration
- [ ] Test redeployment process
- [ ] Document rollback procedure

## Troubleshooting

### Common Issues

**Build fails on Render:**
- Check Dockerfile syntax
- Verify requirements.txt is valid
- Review build logs for specific errors

**Service crashes immediately:**
- Check environment variables are set
- Verify API keys are valid
- Review application logs

**Transcription fails:**
- Verify OpenAI API key has credits
- Check B2 credentials are correct
- Ensure file exists in B2 bucket

**Webhook not received:**
- Verify callback URL is accessible
- Check webhook client logs
- Test callback URL manually

**Out of memory:**
- Upgrade Render plan
- Check for memory leaks
- Consider file size limits

## Success Criteria

Your deployment is successful when:
- ✅ Health endpoint returns 200
- ✅ Authentication works correctly
- ✅ Jobs can be created and queued
- ✅ Media files download from B2
- ✅ Audio extraction works
- ✅ Transcription completes successfully
- ✅ Webhooks are delivered
- ✅ Job status can be queried
- ✅ Errors are handled gracefully
- ✅ Logs show no critical errors

## Support Resources

- **Render Docs:** https://render.com/docs
- **OpenAI API Docs:** https://platform.openai.com/docs
- **Backblaze B2 Docs:** https://www.backblaze.com/b2/docs/
- **FastAPI Docs:** https://fastapi.tiangolo.com
- **Project Issues:** Check GitHub repository issues

---

**Last Updated:** Check git commit history
**Deployment Date:** _____________
**Deployed By:** _____________
**Production URL:** _____________
