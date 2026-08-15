# Update the canary output

This is a small Harbor canary for the agent orchestrator. In the repository
working directory, update `output.txt` so that its complete contents are exactly
the single line:

```text
ready
```

The file must end with the newline shown above. For example, use
`printf 'ready\n' > output.txt`; do not use `echo -n`.

Run the visible verification command before you finish, commit the change, and
end your response with the required `DONE_CLAIM` marker.
