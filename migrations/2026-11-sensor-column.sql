-- Датчик в glucose_readings: Libre и Ottai в одной таблице.
--
-- init_schema() таблицы создаёт, но не меняет, поэтому ALTER — руками, один
-- раз, до деплоя кода с этой колонкой. Старые строки получают 'libre' по
-- DEFAULT, и бот, читающий без фильтра, видит ровно то же, что и раньше, —
-- пока второй датчик не начнёт писать.
--
-- Индекс по timestamp — для чтений без фильтра по датчику (бот до своего
-- обновления): с новым первичным ключом (sensor, timestamp) им пришлось бы
-- сканировать таблицу.
--
-- glucose_sources создаёт init_schema() при старте сборщика или сервера приёма.

ALTER TABLE glucose_readings
  ADD COLUMN sensor VARCHAR(16) NOT NULL DEFAULT 'libre' FIRST,
  ADD COLUMN received_at DATETIME NULL,
  ADD COLUMN direction VARCHAR(32) NULL,
  DROP PRIMARY KEY,
  ADD PRIMARY KEY (sensor, timestamp),
  ADD INDEX ix_glucose_readings_timestamp (timestamp);
