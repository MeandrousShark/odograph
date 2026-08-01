-- Structured business purpose. Purpose is deliberately separate
-- from freeform notes and is human-owned: detector updates never write it.
ALTER TABLE trips ADD COLUMN purpose text;
