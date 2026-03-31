#!/bin/sh

term_handler() {
    kill -TERM "$killpid" 2>/dev/null
    wait "$killpid" 2>/dev/null
}

trap 'term_handler' TERM

miniassistant serve &
killpid="$!"

wait "$killpid"
