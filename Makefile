.PHONY: help quickstart init configure auth start stop restart status logs doctor

help:
	@echo "Targets:"
	@echo "  make quickstart   # guided setup"
	@echo "  make init         # init venv/config/service"
	@echo "  make configure    # write credentials config"
	@echo "  make auth         # run interactive 2FA bootstrap"
	@echo "  make start|stop|restart|status|logs|doctor"

quickstart:
	./icloudctl quickstart

init:
	./icloudctl init

configure:
	./icloudctl configure

auth:
	./icloudctl auth

start:
	./icloudctl start

stop:
	./icloudctl stop

restart:
	./icloudctl restart

status:
	./icloudctl status

logs:
	./icloudctl logs

doctor:
	./icloudctl doctor
