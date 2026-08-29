# Deployment Guide (Free Cloud Tier)

This guide walks you through deploying the GitHub PR Code Reviewer to a 100% free cloud architecture.

## Architecture

Instead of running 7+ local containers, we consolidate the compute into **one single container** that runs both the FastAPI gateway and the Celery worker via `supervisord`.
State and observability are delegated to external managed services with free tiers.

- **Compute:** Render (Web Service, Free Tier, 512MB RAM) or Koyeb
- **PostgreSQL:** Neon.tech (Free Tier)
- **Redis:** Upstash (Free Tier)
- **Vector Database:** Qdrant Cloud (Free Cluster)
- **LLM Tracing:** Langfuse Cloud (Free Tier)
- **LLM Provider:** OpenRouter (Free tier models)

## Prerequisites

1. Set up accounts on:
   - [Neon.tech](https://neon.tech)
   - [Upstash](https://upstash.com)
   - [Qdrant Cloud](https://qdrant.to/cloud)
   - [Langfuse Cloud](https://cloud.langfuse.com)
   - [OpenRouter](https://openrouter.ai)
2. Get your `GITHUB_WEBHOOK_SECRET` and GitHub App details.

## Step 1: External State Setup

1. **Neon PostgreSQL:** Create a project. Get the pooled connection string. E.g., `postgresql+asyncpg://user:pass@ep-rest-of-url.neon.tech/dbname?ssl=require`.
2. **Upstash Redis:** Create a database. Ensure you use the rediss:// connection string for SSL. E.g., `rediss://default:password@endpoint.upstash.io:6379`.
3. **Qdrant Cloud:** Create a free cluster. Get the URL and the API Key.
4. **Langfuse Cloud:** Create a project. Get the Public and Secret API keys. Ensure Host is `https://cloud.langfuse.com`.

## Step 2: Configure Environment Variables

Use the `.env.example` as a template. In your hosting provider's dashboard, configure the following:

- `DATABASE_URL`: Your Neon Postgres URL (make sure it starts with `postgresql+asyncpg://` or `postgres://`).
- `REDIS_URL`: Your Upstash URL.
- `CELERY_BROKER_URL`: Same as your Upstash URL.
- `CELERY_RESULT_BACKEND`: Same as your Upstash URL.
- `QDRANT_URL`: Your Qdrant Cloud Cluster URL.
- `QDRANT_API_KEY`: Your Qdrant Cloud API Key.
- `LANGFUSE_PUBLIC_KEY`: Langfuse Cloud Public Key.
- `LANGFUSE_SECRET_KEY`: Langfuse Cloud Secret Key.
- `LANGFUSE_HOST`: `https://cloud.langfuse.com`.

## Step 3: Deploying on Render

1. Create a new **Web Service** on Render connected to your GitHub repository.
2. Select **Docker** as the Runtime.
3. Set the Dockerfile Path to `Dockerfile.prod`.
4. Ensure the Instance Type is the Free Tier (512MB).
5. Add all the environment variables from Step 2.
6. Click **Deploy**.

## Step 4: Webhook Configuration

After deployment, Render will provide a URL (e.g., `https://your-app.onrender.com`).
1. Go to your GitHub App / Repository Settings.
2. Set the Webhook URL to `https://your-app.onrender.com/api/v1/webhooks/github/`.
3. Verify the delivery status in GitHub!

## Local Testing

If you want to test the free cloud setup locally using the external services:
```bash
docker compose -f docker-compose.free-cloud.yml up --build
```
Ensure your `.env` contains the remote URLs.
