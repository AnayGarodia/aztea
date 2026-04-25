-- 0017: add role column to users table
-- Values: 'builder' (skill builders), 'hirer' (callers), 'both' (default for all existing users)
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'both';
