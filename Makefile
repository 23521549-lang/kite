.PHONY: bootstrap doctor bootstrap-infra

bootstrap:
	bash poc/kaggle_worker/bootstrap.sh

doctor:
	./.venv/bin/kite --config "$${KITE_DATA_DIR:-/var/lib/kite}/kite/config.yaml" doctor

bootstrap-infra:
	bash infra/aws/aws-profile.sh
	bash infra/aws/terraform-bootstrap.sh
	bash infra/aws/github-vars.sh
