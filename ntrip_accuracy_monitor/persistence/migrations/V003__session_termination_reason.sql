-- PostgreSQL
-- ntrip_accuracy_monitor/persistence/migrations/V003__session_termination_reason.sql
--
-- Добавляет колонку termination_reason для различения причин завершения
-- сеанса в БД: штатно, по сигналу оператора, из-за исключения.
-- Связано с SessionRepository.end(session_id, reason).

ALTER TABLE sessions
ADD COLUMN termination_reason TEXT
    CONSTRAINT sessions_termination_reason_values
        CHECK (termination_reason IN ('normal', 'signal', 'error'));

ALTER TABLE sessions
ADD CONSTRAINT sessions_termination_consistency CHECK (
    (ended_at IS NULL  AND termination_reason IS NULL) OR
    (ended_at IS NOT NULL AND termination_reason IS NOT NULL)
);

COMMENT ON COLUMN sessions.termination_reason IS
    'Причина завершения сеанса: normal (штатно), signal (SIGINT/SIGTERM от оператора), error (исключение в TaskGroup).';
