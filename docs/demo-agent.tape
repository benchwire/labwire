# Render with: vhs docs/demo-agent.tape   (brew install vhs)
#
# Records `make demo-pylabrobot-claude`: a Claude agent reads the labwire:deck
# resource, plans a serial dilution, presents an S2 confirmation for every
# liquid move, and needs an operator grant for the S3 gripper move. Ends on
# the signed manifest verifying: labwire verify: OK - authentic.
#
# Requires ANTHROPIC_API_KEY in the environment; without it the demo falls
# back to the scripted dilution. DEMO_FAST trims the dilution to 3 steps and
# DEMO_QUIET silences the chatterbox backend's operation tables (both are
# presentation toggles documented in examples/liquid_handling).
#
# The published clip is this recording retimed by docs/demo-agent-retime.py:
# the live run's API stalls are capped at 2.5 s per screen so the clip stays
# under 45 s. Every frame is the recording's own, in order; only hold times
# change. That script also derives docs/demo-agent.mp4 (vhs 0.11 cannot
# encode mp4 against ffmpeg 8). Full pipeline:
#   vhs docs/demo-agent.tape && python3 docs/demo-agent-retime.py
Output docs/demo-agent.gif

Set Shell "bash"
Set Width 1200
Set Height 675
Set Theme "Catppuccin Mocha"
Set FontSize 20
Set Padding 24
Set TypingSpeed 40ms
Set Framerate 30

# Two timed live runs measured 40 s end to end: deck read at 3 s, a 14 s
# planning pause the viewer spends on the deck JSON, S2 beats every 2 s,
# the S3 refusal at 33 s, operator approval and the granted move by 39 s.
# 1.5x compression brings the whole recording under 35 s.
Set PlaybackSpeed 1.5

Hide
Type "export DEMO_QUIET=1 DEMO_FAST=1 PYTHONUNBUFFERED=1"
Enter
Type `make() { command make "$@" 2>&1 | tee /tmp/demo-agent-take.log; }`
Enter
Type "clear"
Enter
Show

Type "make demo-pylabrobot-claude"
Sleep 600ms
Enter

# Timed live runs measured 40 s end to end, stretching toward 90 s when API
# rate-limit backoff bites; 120 s covers both and the retime script cuts the
# idle tail. (Wait+Screen would be the right tool, but its screen polling
# freezes this vhs release's capture mid-run.)
Sleep 120s

# Hold on the final screen: labwire verify: OK - authentic
Sleep 8s
