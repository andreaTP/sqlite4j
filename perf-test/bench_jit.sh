#!/bin/bash
set -euxo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Run with JIT compilation logging to see which methods are C1 vs C2 compiled
jbang --fresh \
      --java-options="-XX:+UnlockDiagnosticVMOptions" \
      --java-options="-XX:+PrintCompilation" \
      ${SCRIPT_DIR}/test 2>&1 | tee ${SCRIPT_DIR}/jit_log.txt
