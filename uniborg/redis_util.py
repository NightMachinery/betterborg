# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""
Centralized Redis utilities for BetterBorg.

This module provides a shared Redis connection and common operations
used across different plugins and utilities.
"""

import os
from typing import Optional
from datetime import datetime

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

# --- Configuration ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
REDIS_EXPIRE_DURATION = int(os.environ.get("BORG_REDIS_EXPIRE_DURATION", "3600"))  # 1 hour default
REDIS_LONG_EXPIRE_DURATION = int(os.environ.get("BORG_REDIS_LONG_EXPIRE_DURATION", "2592000"))  # 1 month default
FALLBACK_TO_MEMORY = True  # Fallback to in-memory storage if Redis fails

# --- Connection Management ---
_redis_client: Optional[redis.Redis] = None

async def get_redis() -> Optional[redis.Redis]:
    """Get or create Redis connection."""
    global _redis_client
    if not REDIS_AVAILABLE:
        return None
        
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            # Test connection
            await _redis_client.ping()
        except Exception as e:
            print(f"RedisUtil: Failed to connect to Redis: {e}")
            if not FALLBACK_TO_MEMORY:
                raise
            return None
    
    return _redis_client

async def close_redis():
    """Close Redis connection if it exists."""
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None

# --- Key Generation Helpers ---

def chat_history_key(chat_id: int) -> str:
    """Redis key for chat history list."""
    return f"borg:history:chat:{chat_id}"

def message_lookup_key(message_id: int) -> str:
    """Redis key for message to chat ID lookup."""
    return f"borg:history:lookup:{message_id}"

def file_cache_key(file_id: str) -> str:
    """Redis key for file cache."""
    return f"borg:files:{file_id}"

def smart_context_key(user_id: int) -> str:
    """Redis key for smart context state."""
    return f"borg:smart_context:{user_id}"

# --- Common Redis Operations ---

async def set_with_expiry(key: str, value: str, *, expire_seconds: int = None) -> bool:
    """Set a key-value pair with optional expiration."""
    redis_client = await get_redis()
    if not redis_client:
        return False
    
    try:
        await redis_client.set(key, value, ex=expire_seconds or REDIS_EXPIRE_DURATION)
        return True
    except Exception as e:
        print(f"RedisUtil: Failed to set key {key}: {e}")
        return False

async def get_and_renew(key: str, *, expire_seconds: int = None) -> Optional[str]:
    """Get a value and renew its expiration."""
    redis_client = await get_redis()
    if not redis_client:
        return None
    
    try:
        value = await redis_client.get(key)
        if value:
            # Renew expiry on access
            await redis_client.expire(key, expire_seconds or REDIS_EXPIRE_DURATION)
        return value
    except Exception as e:
        print(f"RedisUtil: Failed to get key {key}: {e}")
        return None

async def delete_key(key: str) -> bool:
    """Delete a key from Redis."""
    redis_client = await get_redis()
    if not redis_client:
        return False
    
    try:
        await redis_client.delete(key)
        return True
    except Exception as e:
        print(f"RedisUtil: Failed to delete key {key}: {e}")
        return False

async def expire_key(key: str, *, expire_seconds: int = None) -> bool:
    """Set expiration time for a key."""
    redis_client = await get_redis()
    if not redis_client:
        return False
    
    try:
        await redis_client.expire(key, expire_seconds or REDIS_EXPIRE_DURATION)
        return True
    except Exception as e:
        print(f"RedisUtil: Failed to set expiry for key {key}: {e}")
        return False

# --- Hash Operations (for file caching) ---

async def hset_with_expiry(key: str, field_values: dict, *, expire_seconds: int = None) -> bool:
    """Set multiple hash fields with expiration."""
    redis_client = await get_redis()
    if not redis_client:
        return False
    
    try:
        pipe = redis_client.pipeline()
        for field, value in field_values.items():
            pipe.hset(key, field, value)
        pipe.expire(key, expire_seconds or REDIS_EXPIRE_DURATION)
        await pipe.execute()
        return True
    except Exception as e:
        print(f"RedisUtil: Failed to set hash {key}: {e}")
        return False

async def hgetall_and_renew(key: str, *, expire_seconds: int = None) -> Optional[dict]:
    """Get all hash fields and renew expiration."""
    redis_client = await get_redis()
    if not redis_client:
        return None
    
    try:
        data = await redis_client.hgetall(key)
        if data:
            # Renew expiry on access
            await redis_client.expire(key, expire_seconds or REDIS_EXPIRE_DURATION)
        return data if data else None
    except Exception as e:
        print(f"RedisUtil: Failed to get hash {key}: {e}")
        return None

# --- Sorted Set Operations (for history) ---

async def zadd_with_limit_and_expiry(key: str, score_member_pairs: dict, *, limit: int = None, expire_seconds: int = None) -> bool:
    """Add to sorted set with size limit and expiration."""
    redis_client = await get_redis()
    if not redis_client:
        return False
    
    try:
        pipe = redis_client.pipeline()
        # Add members
        pipe.zadd(key, score_member_pairs)
        # Maintain size limit by removing oldest entries
        if limit:
            pipe.zremrangebyrank(key, 0, -(limit + 1))
        # Set expiry
        pipe.expire(key, expire_seconds or REDIS_EXPIRE_DURATION)
        await pipe.execute()
        return True
    except Exception as e:
        print(f"RedisUtil: Failed to add to sorted set {key}: {e}")
        return False

async def zrange_and_renew(key: str, start: int = 0, end: int = -1, *, expire_seconds: int = None) -> list:
    """Get sorted set range and renew expiration."""
    redis_client = await get_redis()
    if not redis_client:
        return []
    
    try:
        data = await redis_client.zrange(key, start, end)
        if data:
            await redis_client.expire(key, expire_seconds or REDIS_EXPIRE_DURATION)
        return data
    except Exception as e:
        print(f"RedisUtil: Failed to get sorted set range {key}: {e}")
        return []

# --- Utility Functions ---

def is_redis_available() -> bool:
    """Check if Redis is available for use."""
    return REDIS_AVAILABLE

def get_expire_duration() -> int:
    """Get the default expiration duration."""
    return REDIS_EXPIRE_DURATION

def get_long_expire_duration() -> int:
    """Get the long expiration duration (1 month)."""
    return REDIS_LONG_EXPIRE_DURATION