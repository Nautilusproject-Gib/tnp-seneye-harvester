-- Seagrass nursery monitoring schema (MySQL)
-- The harvester creates these itself on first run; this file is here so the
-- tables can be reviewed, or created by hand on a managed database.

CREATE TABLE IF NOT EXISTS devices (
    device_id   VARCHAR(64) PRIMARY KEY,          -- Seneye device id
    description TEXT,
    device_type INTEGER,                   -- 1 aquarium, 2 pond, 3 reef
    sump_code   TEXT,                      -- SA12, SA345, SB12, ...
    system_code TEXT,                      -- A, B, C, D, E
    label       TEXT,
    first_seen  BIGINT,
    last_seen   BIGINT
);

CREATE TABLE IF NOT EXISTS readings (
    device_id    VARCHAR(64) NOT NULL,
    reading_time BIGINT NOT NULL,          -- unix seconds, from the device
    fetched_at   BIGINT NOT NULL,          -- unix seconds, when we polled
    temperature  DOUBLE,         -- degrees C
    ph           DOUBLE,
    nh3          DOUBLE,         -- free ammonia, mg/L
    nh4          DOUBLE,
    o2           DOUBLE,
    par          DOUBLE,
    lux          DOUBLE,
    kelvin       DOUBLE,
    temperature_status INTEGER,
    ph_status    INTEGER,
    nh3_status   INTEGER,
    nh4_status   INTEGER,
    o2_status    INTEGER,
    par_status   INTEGER,
    lux_status   INTEGER,
    kelvin_status INTEGER,
    slide_serial TEXT,
    slide_expires BIGINT,
    out_of_water INTEGER,
    disconnected INTEGER,
    PRIMARY KEY (device_id, reading_time)
);

-- MySQL has no CREATE INDEX IF NOT EXISTS; create it once:
-- CREATE INDEX idx_readings_time ON readings (reading_time);

CREATE TABLE IF NOT EXISTS harvest_runs (
    run_id            INT AUTO_INCREMENT PRIMARY KEY,
    started_at        BIGINT,
    finished_at       BIGINT,
    status            TEXT,
    devices_polled    INTEGER,
    readings_inserted INTEGER,
    message           TEXT
);

-- Daily statistics, if you would rather compute them in the database than in
-- the exporter:
--
-- SELECT device_id,
--        FROM_UNIXTIME(reading_time - MOD(reading_time, 86400)) AS day,
--        count(temperature) AS n,
--        min(temperature)  AS t_min,
--        max(temperature)  AS t_max,
--        avg(temperature)  AS t_mean,
--        STDDEV_SAMP(temperature) AS t_sd
-- FROM readings
-- GROUP BY 1, 2
-- ORDER BY 1, 2;
