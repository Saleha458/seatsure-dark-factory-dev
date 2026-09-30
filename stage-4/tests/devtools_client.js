const readline = require("node:readline");

const socket = new WebSocket(process.argv[2]);
const input = readline.createInterface({ input: process.stdin });
const pending = new Map();

socket.addEventListener("open", () => process.stdout.write('{"ready":true}\n'));
socket.addEventListener("message", (event) => {
  const response = JSON.parse(event.data);
  if (response.id === undefined) return;
  if (!pending.has(response.id)) return;
  const timer = pending.get(response.id);
  clearTimeout(timer);
  pending.delete(response.id);
  process.stdout.write(JSON.stringify(response) + "\n");
});
socket.addEventListener("error", () => {
  process.stdout.write('{"error":{"message":"DevTools WebSocket error"}}\n');
});

input.on("line", (line) => {
  const command = JSON.parse(line);
  const timer = setTimeout(() => {
    pending.delete(command.id);
    process.stdout.write(JSON.stringify({
      id: command.id, error: { message: "DevTools command timed out" },
    }) + "\n");
  }, 15000);
  pending.set(command.id, timer);
  socket.send(JSON.stringify(command));
});
