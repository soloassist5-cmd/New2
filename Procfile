# Запасная команда запуска для нативного (не докерного) сервиса на Render и
# похожих хостах. В render.yaml сервис описан как docker — там запуск берётся
# из Dockerfile, и этот файл не используется. Но если сервис создан руками как
# «Python», Render подставляет свою заглушку `$ gunicorn your_application.wsgi`
# и падает с «bash: line 1: $: command not found». Procfile это перекрывает.
web: python main.py
