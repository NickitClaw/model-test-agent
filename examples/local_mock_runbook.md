# Local Mock Benchmark Runbook

1. Open one terminal for the mock server.
2. In that terminal, launch `python3 examples/mock_server.py`.
3. Wait until the output contains `SERVER READY`.
4. Open another terminal for the client.
5. Run `python3 examples/mock_client.py`.
6. After the client finishes, open an interactive write session with `cat > examples/mock_notes.txt`.
7. Type two summary lines and send EOF.
8. Verify that the notes file contains the expected summary.
9. Stop the server with `Ctrl-C`.
