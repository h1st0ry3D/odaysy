.pragma library

// Pure helpers shared by the panel. Nothing here touches a file, a process or
// the shell, so the rules for the bar value and calendar days can be read and
// tested on their own.

var MAX_TITLE = 120
var MAX_EVENTS = 100

// Days elapsed between the local calendar date of `iso` and the local calendar
// date of `nowMs`. These are calendar days, not 24-hour periods, so an event
// added at 23:00 becomes 1 at midnight. The two dates are rebuilt in UTC so a
// DST change cannot make a day 23 or 25 hours long and round to the wrong
// number.
function daysSince(iso, nowMs) {
  var then = new Date(String(iso === undefined || iso === null ? "" : iso))
  var now = new Date(Number(nowMs))
  if (!isFinite(then.getTime()) || !isFinite(now.getTime())) return 0
  var start = Date.UTC(then.getFullYear(), then.getMonth(), then.getDate())
  var end = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())
  return Math.max(0, Math.round((end - start) / 86400000))
}

// The event whose counter belongs in the bar. One event is shown without
// asking; with several, the one the user pinned. With several events and no
// pin, there is no number the bar can show, so it shows the empty mark.
function barEntry(events, pinnedId) {
  if (!Array.isArray(events) || events.length === 0) return null
  if (events.length === 1) return events[0]
  var id = String(pinnedId === undefined || pinnedId === null ? "" : pinnedId)
  for (var index = 0; index < events.length; index++) {
    if (String(events[index].id) === id) return events[index]
  }
  return null
}

function barText(events, pinnedId, nowMs) {
  var entry = barEntry(events, pinnedId)
  return entry ? String(daysSince(entry.since, nowMs)) : "∞"
}

function dayLabel(days) {
  var value = Math.max(0, Math.floor(Number(days) || 0))
  return value === 1 ? "1 day" : value + " days"
}

// "28 Oct 2026", in the machine's local time. The stored value is UTC; a date
// is what a person reads, so it is the local date that is shown.
var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

function sinceLabel(iso) {
  var date = new Date(String(iso === undefined || iso === null ? "" : iso))
  if (!isFinite(date.getTime())) return "an unknown date"
  return date.getDate() + " " + MONTHS[date.getMonth()] + " " + date.getFullYear()
}

function countLabel(count) {
  var value = Math.max(0, Math.floor(Number(count) || 0))
  return value === 1 ? "1 event" : value + " events"
}

// Text handed to a component the plugin does not draw itself cannot set
// textFormat. That covers the bar tooltip, PanelHero, a Button label and
// ConfirmDialog. Those host components use Text.AutoText, which renders a
// string that looks like markup as rich text, and rich text can load a URL.
// This strips the three characters that can begin markup, along with control
// and bidirectional-override characters, and caps the result. Text the plugin
// draws itself uses Text.PlainText instead.
function plain(value, limit) {
  var source = String(value === undefined || value === null ? "" : value)
  var maximum = Math.max(1, Math.floor(Number(limit) || 80))
  var out = ""
  for (var index = 0; index < source.length && out.length < maximum; index++) {
    var code = source.charCodeAt(index)
    var character = source[index]
    if (code === 9 || code === 10 || code === 13) {
      if (out !== "" && !/\s$/.test(out)) out += " "
      continue
    }
    if (code < 0x20 || (code >= 0x7f && code <= 0x9f)) continue
    if (code >= 0x200b && code <= 0x200f) continue
    if (code >= 0x202a && code <= 0x202e) continue
    if (code >= 0x2060 && code <= 0x2069) continue
    if (code === 0xfeff) continue
    if (character === "<" || character === ">" || character === "&") continue
    out += character
  }
  return out.trim()
}
