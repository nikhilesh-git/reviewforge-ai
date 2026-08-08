-- PostgreSQL initialization script
-- Runs once when the postgres container is first created.
-- Creates extensions and initial configuration.

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable faster JSON operations
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Set timezone
SET timezone = 'UTC';

-- Log initialization
DO $$
BEGIN
    RAISE NOTICE 'PR Reviewer database initialized with extensions: uuid-ossp, pg_trgm';
END $$;
