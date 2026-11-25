git -c core.quotepath=false diff --no-ext-diff --no-color --no-renames -U0 HEAD -- . \
  ':(exclude).cb_*' ':(exclude)patch.txt' | grep -E '^(diff --git|@@ )'
echo '--FP--'
{ git diff --no-ext-diff --no-color --binary HEAD -- . ':(exclude).cb_*' ':(exclude)patch.txt'
  git ls-files -o --exclude-standard -z -- . ':(exclude).cb_*' | xargs -0 -r git hash-object -- ; } \
  | sha256sum | cut -c1-64
echo '--NUM--'
git diff --no-ext-diff --numstat HEAD -- . ':(exclude).cb_*' ':(exclude)patch.txt' \
  | awk '{a+=$1; d+=$2} END {print a+0, d+0}'
