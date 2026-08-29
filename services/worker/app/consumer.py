"""Redis Stream consumer that bridges events to Celery.

This daemon continuously reads from the Redis stream `pr:events` using
consumer groups, and dispatches each event to the Celery `review` queue.
It ensures that events produced by the gateway are picked up by the worker.
"""

import asyncio
import logging
import socket

from shared.infrastructure.logging import configure_logging
from shared.infrastructure.redis_client import (
    close_redis,
    ensure_consumer_group,
    get_redis_client,
    init_redis,
    stream_acknowledge,
    stream_read_group,
)
from shared.domain.events import PRReviewRequestedEvent

from .config import get_settings
from .tasks import review_pr

logger = logging.getLogger(__name__)


async def run_consumer() -> None:
    """Run the main consumer loop."""
    settings = get_settings()

    # Configure structured logging
    configure_logging(
        log_level=settings.log_level,
        json_output=settings.is_production,
        service_name="pr-review-consumer",
    )

    logger.info("Initializing Redis for consumer daemon")
    
    # Initialize the redis connection
    init_redis(redis_url=settings.redis_url, max_connections=10)
    client = get_redis_client()

    stream_name = settings.redis_stream_name
    group_name = settings.redis_stream_consumer_group
    
    # Use the container hostname as the consumer name for unique tracking
    consumer_name = f"consumer-{socket.gethostname()}"

    # Ensure the stream and consumer group exist before reading
    await ensure_consumer_group(client, stream_name, group_name)

    logger.info(
        "Started Redis Stream Consumer",
        extra={
            "stream": stream_name,
            "group": group_name,
            "consumer": consumer_name,
        },
    )

    try:
        while True:
            try:
                # Block for up to 5 seconds waiting for new events
                entries = await stream_read_group(
                    client,
                    stream_name,
                    group_name,
                    consumer_name,
                    count=10,
                    block_ms=5000,
                )
                
                for entry_id, fields in entries:
                    logger.info(
                        "Received stream event, dispatching to Celery",
                        extra={"entry_id": entry_id},
                    )
                    
                    # Parse the Redis stream format into the domain event
                    event = PRReviewRequestedEvent.from_redis_dict(fields)
                    
                    # Dispatch directly to Celery worker queue (Celery task expects a flat dict)
                    review_pr.apply_async(args=[event.model_dump()], queue="review")
                    
                    # Acknowledge the message so it isn't redelivered
                    await stream_acknowledge(client, stream_name, group_name, entry_id)
                    
                    logger.info(
                        "Acknowledged stream event",
                        extra={"entry_id": entry_id},
                    )
            
            except Exception as exc:
                logger.error("Consumer loop error", extra={"error": str(exc)})
                await asyncio.sleep(5)  # Backoff on error
                
    finally:
        logger.info("Consumer shutting down")
        await close_redis()


if __name__ == "__main__":
    try:
        asyncio.run(run_consumer())
    except KeyboardInterrupt:
        pass
