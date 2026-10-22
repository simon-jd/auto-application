# deploy/grokbot/userns-profiles.awk, used by setup.sh and check.sh:
#   awk -v prog="/opt/Grok Bot/grok-bot" -f userns-profiles.awk /etc/apparmor.d/FILE
# If FILE has a profile that attaches to the program prog (quoted or as a whole word in the profile's header) and FILE
# allows user namespaces (a "userns," or "userns create," rule that is not "deny"), prints the name of each such
# profile and exits 0; otherwise prints nothing and exits 1. The userns rule is looked for in the whole file; a profile
# file holds the profile of one program. Profiles of other programs that only mention prog in a rule do not match.
function trim(s) {
  sub(/^[ \t]+/, "", s)
  sub(/[ \t]+$/, "", s)
  return s
}
# The name of the profile whose header (the text before "{") is h, if it attaches to prog; otherwise "".
function attached_name(h,    n, i, word, rest) {
  h = trim(h)
  if (index(h, "\"" prog "\"") == 0) {
    n = split(h, word, /[ \t]+/)
    for (i = 1; i <= n; i++) if (word[i] == prog) break
    if (i > n) return ""
  }
  if (h !~ /^profile[ \t]/) return prog  # a profile named by its path
  rest = trim(substr(h, 8))
  if (substr(rest, 1, 1) == "\"") {
    rest = substr(rest, 2)
    return substr(rest, 1, index(rest, "\"") - 1)
  }
  split(rest, word, /[ \t]+/)
  return word[1]
}
/^[ \t]*#/ { next }
{
  line = $0
  brace = index(line, "{")
  if (brace > 0) {
    name = attached_name(substr(line, 1, brace - 1))
    if (name != "") names[++count] = name
    line = substr(line, brace + 1)
  }
  if (line ~ /(^|[{,])[ \t]*((audit|allow)[ \t]+)*userns[ \t]*(create[ \t]*)?,/) userns = 1
}
END {
  if (count == 0 || !userns) exit 1
  for (i = 1; i <= count; i++) print names[i]
}
