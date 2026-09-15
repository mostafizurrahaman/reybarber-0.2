const port = process.env.PORT || 8083;

module.exports = {
  apps: [
    {
      name: 'reybarber-0.2',
      script: 'venv/bin/gunicorn',
      args: `-b 0.0.0.0:${port} -w 2 --timeout 300 app:app`,
      interpreter: 'none',
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
    },
  ],
};
