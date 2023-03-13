#!/usr/bin/env bash

set -o errtrace -o pipefail -o errexit

TEST_SPLITS="${TEST_SPLITS:-1}"
TEST_GROUP="${TEST_GROUP:-1}"

eval "$(sudo python -m conda init bash --dev)"
conda info
conda-build tests/test-recipes/activate_deactivate_package
python -m pytest --cov=conda --store-durations --durations-path=tests/durations/${OS}.json --splitting-algorithm=least_duration -m "integration" -v --splits ${TEST_SPLITS} --group=${TEST_GROUP}
python -m conda.common.io
