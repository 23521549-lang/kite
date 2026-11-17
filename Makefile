.PHONY: bootstrap doctor

bootstrap:
	bash poc/kaggle_worker/bootstrap.sh

doctor:
	./.venv/bin/kite --config "$${KITE_DATA_DIR:-/var/lib/kite}/kite/config.yaml" doctor
