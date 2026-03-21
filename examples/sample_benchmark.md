# Example Benchmark Runbook

1. Open one terminal for the server.
2. In the server terminal, `cd /workspace/vllm`.
3. Launch the OpenAI-compatible model server with Docker:

   ```bash
   docker run --rm -it --gpus all -p 8000:8000 \
     -v /models:/models \
     vllm/vllm-openai:latest \
     --model /models/Qwen2.5-7B-Instruct
   ```

4. Wait until the server log contains `Application startup complete`.
5. Open another terminal on the remote benchmark box with SSH: `ssh -tt ubuntu@10.0.0.15`.
6. In the remote terminal:

   ```bash
   cd /bench
   source .venv/bin/activate
   python benchmark_serving.py \
     --backend openai-chat \
     --base-url http://10.0.0.2:8000/v1 \
     --model Qwen2.5-7B-Instruct
   ```

7. If the benchmark output contains `Traceback` or `Connection refused`, stop and collect logs.
8. After the benchmark is done, open `vim notes.md`, insert the summary line, save, and quit.
