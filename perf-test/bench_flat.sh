#!/bin/bash
set -euxo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Run with async-profiler in collapsed stack format for programmatic analysis
jbang --fresh --deps org.openjdk.jmh:jmh-generator-annprocess:1.36 \
      --javaagent=ap-loader@jvm-profiling-tools/ap-loader=start,event=cpu,file=${SCRIPT_DIR}/profile_collapsed.txt,collapsed \
      ${SCRIPT_DIR}/test
