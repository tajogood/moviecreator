#!/bin/bash
# Finder 에서 더블클릭하면 MovieCreator 웹 서버가 켜집니다.
cd "$(dirname "$0")" && exec ./web.sh "$@"
