# Neon Nodes
Clients for connecting to a server running Hana. These are minimal classes that
are responsible for collecting a user's input, sending it to a remote system for
processing, and presenting a response to the user.

## Voice Client
The voice client will start a service that listens for a wake word on the local
system, sends recorded speech to a HANA endpoint for processing, and plays back
the response.

## Websocket Client
The websocket client starts a local listener service and establishes a websocket
connection to a remove HANA server. Compared to the Voice Client, this has 
lower latency and allows for asynchronous messages from the HANA server.

## Configuration
This service is configured via `~/.config/neon/neon.yaml`.

```yaml
neon_node:
  description: Neon Node  # Friendly description of the node
  hana_address: https://hana.neonaiservices.com  # Hana server HTTP address
  hana_username: node_user  # Hana node user username
  hana_password: node_password  # Hana node user password
```
