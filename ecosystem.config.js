// Три процесса: два источника пишут показания в базу, публикатор по основному
// датчику собирает снимок страницы, тревогу и метку свежести.
//
// Общее у всех: TZ=UTC — метки времени в БД UTC; сутки режутся в DISPLAY_TZ
// (на странице — HOME_TIMEZONE в web/js/format.js), часы страница подписывает
// в поясе, выбранном в шапке. Процессу локальная зона не нужна ни для чего.
// exp_backoff_restart_delay: 100ms, 200ms, 400ms ... pm2 ограничивает паузу
// 15000ms, так что рестарты оседают на одной попытке в 15 с. max_restarts —
// без предела вместо 16 по умолчанию.
const common = {
  interpreter: "./venv/bin/python",
  cwd: __dirname,
  autorestart: true,
  exp_backoff_restart_delay: 100,
  max_restarts: Number.MAX_SAFE_INTEGER,
  env: {
    TZ: "UTC",
  },
};

module.exports = {
  apps: [
    {
      // Источник Libre: опрос LibreLinkUp (libre.py). Один на парк: Abbott
      // выдаёт одну сессию на учётку.
      name: "glucose-libre",
      script: "libre.py",
      ...common,
    },
    {
      // Источник по Nightscout API v1 (nightscout.py). Слушает
      // 127.0.0.1:NIGHTSCOUT_PORT, наружу его выставляет nginx на /api/v1/.
      name: "glucose-nightscout",
      script: "nightscout.py",
      args: "serve",
      ...common,
    },
    {
      // Снимок, тревога и метка свежести — по основному датчику (publisher.py).
      // Один на парк: состояние эпизода тревоги лежит в .alerts.json.
      name: "glucose-publisher",
      script: "publisher.py",
      ...common,
    },
  ],
};
