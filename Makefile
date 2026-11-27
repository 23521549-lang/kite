.PHONY: bootstrap doctor bootstrap-infra lock

bootstrap:
	bash poc/kaggle_worker/bootstrap.sh

doctor:
	./.venv/bin/kite --config "$${KITE_DATA_DIR:-/var/lib/kite}/kite/config.yaml" doctor

bootstrap-infra:
	bash infra/aws/aws-profile.sh
	bash infra/aws/terraform-bootstrap.sh
	bash infra/aws/github-vars.sh

# requirements.lock: the versions `kite update` and the bootstrap install (uv pip install -c). Linux
# x86_64 and Python 3.12, like the machines. Pins already in the file are kept unless pyproject.toml needs
# others (CI checks the file is current); delete the file first to move everything to the newest versions.
LOCK ?= requirements.lock
lock:
	uv pip compile -q pyproject.toml --extra codemap --extra kaggle --extra browser --extra web --extra ui \
	  --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --custom-compile-command "make lock" \
	  -o $(LOCK)
