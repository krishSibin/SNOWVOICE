// Business Rule: after insert/update on incident.
// Signs the payload with a shared secret and calls the agent, which queues the
// work and returns immediately.
(function executeRule(current, previous /*null when async*/) {

    var endpoint = gs.getProperty('x_ai_agent.endpoint');      // https://agent.internal/webhook/servicenow
    var secret = gs.getProperty('x_ai_agent.webhook_secret');
    if (!endpoint || !secret) {
        gs.warn('[ai-agent] endpoint or secret property missing, skipping');
        return;
    }

    var body = new global.JSON().encode({
        number: current.number.toString(),
        sys_id: current.sys_id.toString(),
        table: 'incident',
        priority: current.priority.toString(),
        event: current.isNewRecord() ? 'created' : 'updated'
    });

    var mac = new GlideCertificateEncryption();
    var signature = mac.generateMac(secret, 'HmacSHA256', body); // base64
    var hexSignature = global.GlideStringUtil.base64DecodeAsBytes
        ? bytesToHex(global.GlideStringUtil.base64DecodeAsBytes(signature))
        : signature;

    var request = new sn_ws.RESTMessageV2();
    request.setEndpoint(endpoint);
    request.setHttpMethod('POST');
    request.setRequestHeader('Content-Type', 'application/json');
    request.setRequestHeader('X-SN-Signature', hexSignature);
    request.setRequestBody(body);
    request.setHttpTimeout(5000);

    try {
        var response = request.execute();
        if (response.getStatusCode() >= 300) {
            gs.warn('[ai-agent] ' + current.number + ' -> ' + response.getStatusCode() + ' ' + response.getBody());
        }
    } catch (err) {
        gs.error('[ai-agent] call failed for ' + current.number + ': ' + err);
    }

    function bytesToHex(bytes) {
        var hex = '';
        for (var i = 0; i < bytes.length; i++) {
            var value = (bytes[i] + 256) % 256;
            hex += ('0' + value.toString(16)).slice(-2);
        }
        return hex;
    }

})(current, previous);
