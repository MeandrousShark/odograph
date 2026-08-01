-- Durable dedup for all email digests, same role migration 009's
-- nudge_delivery_windows and 010's odometer_reminder_windows play for ntfy:
-- a restart or briefly overlapping replica must not re-send. One table with a
-- kind column (unlike the deliberately separate ntfy ledgers) because this is
-- the generalizing case: two near-identical ledgers made sense for the first
-- two ntfy reminders, but a third-through-sixth kind warrants generalizing
-- instead of copying the pattern again.
CREATE TABLE email_deliveries (
    kind         text NOT NULL CHECK (kind IN
                   ('weekly_nudge', 'monthly_summary',
                    'filing_reminder', 'quarterly_odometer')),
    period_end   timestamptz NOT NULL,
    sent         boolean NOT NULL,  -- false: evaluated, nothing to send
    delivered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, period_end)
);
