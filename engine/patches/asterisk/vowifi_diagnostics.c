/* Opt-in VoWiFi evidence, independent of the raw SIP logger. No bodies or arbitrary
 * header strings are ever written. The private marker expires without a worker. */
#include "asterisk/json.h"
#include "asterisk/utils.h"
#include "asterisk/lock.h"
#include <arpa/inet.h>
#include <ctype.h>
#include <sys/stat.h>

#define MDD_TRACE_DIR "/logs/vowifi-trace"
AST_MUTEX_DEFINE_STATIC(mdd_trace_lock);

static void mdd_trace_string(struct ast_json *obj, const char *key, const char *value)
{
	ast_json_object_set(obj, key, ast_json_string_create(value));
}

static void mdd_trace_int(struct ast_json *obj, const char *key, long long value)
{
	ast_json_object_set(obj, key, ast_json_integer_create(value));
}

static void mdd_trace_label(char *out, const char *prefix, const char *salt, const pj_str_t *value)
{
	char input[640], digest[41];
	int i, offset;
	snprintf(input, sizeof(input), "%s/%.*s", salt, (int) (value->slen > 512 ? 512 : value->slen), value->ptr ? value->ptr : "");
	ast_sha1_hash(digest, input);
	offset = sprintf(out, "%s-", prefix);
	for (i = 0; i < 16; ++i) {
		out[offset + i] = 'a' + (digest[i] <= '9' ? digest[i] - '0' : digest[i] - 'a' + 10);
	}
	out[offset + 16] = '\0';
}

static void mdd_trace_host(struct ast_json *obj, const char *key, const char *salt, pj_str_t host)
{
	char text[256], canonical[INET6_ADDRSTRLEN], label[40];
	unsigned char address[16];
	const char *prefix = "host";
	if (host.slen <= 0 || host.slen >= sizeof(text)) {
		return;
	}
	snprintf(text, sizeof(text), "%.*s", (int) host.slen, host.ptr);
	if (inet_pton(AF_INET, text, address) == 1) {
		inet_ntop(AF_INET, address, canonical, sizeof(canonical));
		host = pj_str(canonical);
		prefix = "ip";
	} else if (inet_pton(AF_INET6, text, address) == 1) {
		inet_ntop(AF_INET6, address, canonical, sizeof(canonical));
		host = pj_str(canonical);
		prefix = "ip";
	}
	mdd_trace_label(label, prefix, salt, &host);
	mdd_trace_string(obj, key, label);
}

static struct ast_json *mdd_trace_address(const pj_sockaddr *address, const char *salt)
{
	char buffer[PJ_INET6_ADDRSTRLEN + 16];
	struct ast_json *obj = ast_json_object_create();
	pj_str_t host;
	pj_sockaddr_print(address, buffer, sizeof(buffer), 0);
	host = pj_str(buffer);
	mdd_trace_host(obj, "address", salt, host);
	mdd_trace_int(obj, "port", pj_sockaddr_get_port(address));
	return obj;
}

static struct ast_json *mdd_trace_uri(pjsip_uri *raw, const char *salt)
{
	pjsip_sip_uri *uri;
	struct ast_json *obj;
	char label[40];
	if (!raw) {
		return NULL;
	}
	raw = pjsip_uri_get_uri(raw);
	if (!PJSIP_URI_SCHEME_IS_SIP(raw) && !PJSIP_URI_SCHEME_IS_SIPS(raw)) {
		return NULL;
	}
	uri = (pjsip_sip_uri *) raw;
	obj = ast_json_object_create();
	mdd_trace_host(obj, "address", salt, uri->host);
	mdd_trace_label(label, "user", salt, &uri->user);
	mdd_trace_string(obj, "user", label);
	mdd_trace_int(obj, "port", uri->port);
	return obj;
}

static const char *mdd_trace_method(const pj_str_t *method)
{
	static const char *allowed[] = { "REGISTER", "INVITE", "ACK", "CANCEL", "BYE",
		"MESSAGE", "OPTIONS", "SUBSCRIBE", "NOTIFY", "PRACK", "UPDATE", "INFO", "REFER", "PUBLISH" };
	int i;
	for (i = 0; i < ARRAY_LEN(allowed); ++i) {
		if (!pj_stricmp2(method, allowed[i])) {
			return allowed[i];
		}
	}
	return "OTHER";
}

static void mdd_trace_security(struct ast_json *obj, pjsip_msg *msg, const char *name, const char *key)
{
	pj_str_t header_name = pj_str((char *) name);
	pjsip_generic_string_hdr *header = pjsip_msg_find_hdr_by_name(msg, &header_name, NULL);
	static const char *names[] = { "port-c", "port-s", "spi-c", "spi-s" };
	static const char *keys[] = { "port_c", "port_s", "spi_c", "spi_s" };
	char text[1025], token[16], *found, *end;
	struct ast_json *values;
	int i;
	unsigned long value;
	if (!header || header->hvalue.slen <= 0 || header->hvalue.slen > 1024) {
		return;
	}
	snprintf(text, sizeof(text), "%.*s", (int) header->hvalue.slen, header->hvalue.ptr);
	values = ast_json_object_create();
	for (i = 0; i < 4; ++i) {
		snprintf(token, sizeof(token), "%s=", names[i]);
		found = strcasestr(text, token);
		if (!found || !isdigit((unsigned char) found[strlen(token)])) {
			continue;
		}
		value = strtoul(found + strlen(token), &end, 10);
		if ((*end && *end != ';' && *end != ',' && !isspace((unsigned char) *end))
			|| value > (i < 2 ? 65535UL : 0xFFFFFFFFUL)) {
			continue;
		}
		mdd_trace_int(values, keys[i], value);
	}
	ast_json_object_set(obj, key, values);
}

static void mdd_trace_sip(pjsip_msg *msg, pjsip_transport *transport,
	const pj_sockaddr *peer, const char *direction)
{
	char marker[PATH_MAX], salt[65], session[41], label[40], path[PATH_MAX], old[PATH_MAX];
	const char *run = getenv("MDD_RUNDIR");
	const char *transport_name = "other";
	long long expires;
	FILE *file;
	time_t now = time(NULL);
	struct ast_json *record, *contacts, *item;
	pjsip_cid_hdr *cid;
	pjsip_cseq_hdr *cseq;
	pjsip_contact_hdr *contact = NULL;
	pjsip_expires_hdr *expiry;
	struct stat info;
	char *serialized;
	int i;
	if (!msg || !transport || !peer) {
		return;
	}
	snprintf(marker, sizeof(marker), "%s/vowifi-trace.enabled", run ? run : "/run/mdd-sim-gateway");
	file = fopen(marker, "r");
	if (!file) {
		return;
	}
	i = fscanf(file, "%lld %64s %40s", &expires, salt, session);
	fclose(file);
	if (i != 3 || expires <= now || expires > now + 3605 || strlen(salt) != 64
		|| strspn(salt, "0123456789abcdef") != 64
		|| strspn(session, "abcdefghijklmnopqrstuvwxyz0123456789-") != strlen(session)) {
		return;
	}
	cseq = pjsip_msg_find_hdr(msg, PJSIP_H_CSEQ, NULL);
	if (!cseq) {
		return;
	}
	record = ast_json_object_create();
	mdd_trace_string(record, "kind", "sip");
	mdd_trace_string(record, "session", session);
	mdd_trace_int(record, "ts", now);
	mdd_trace_string(record, "direction", direction);
	mdd_trace_string(record, "method", mdd_trace_method(&cseq->method.name));
	mdd_trace_int(record, "cseq", cseq->cseq);
	mdd_trace_int(record, "code", msg->type == PJSIP_RESPONSE_MSG ? msg->line.status.code : 0);
	if (!strcasecmp(transport->type_name, "TCP") || !strcasecmp(transport->type_name, "TCP6")) {
		transport_name = "tcp";
	} else if (!strcasecmp(transport->type_name, "UDP") || !strcasecmp(transport->type_name, "UDP6")) {
		transport_name = "udp";
	} else if (!strcasecmp(transport->type_name, "WS") || !strcasecmp(transport->type_name, "WSS")) {
		transport_name = "ws";
	} else if (!strcasecmp(transport->type_name, "TLS") || !strcasecmp(transport->type_name, "TLS6")) {
		transport_name = "tls";
	}
	mdd_trace_string(record, "transport", transport_name);
	ast_json_object_set(record, "local", mdd_trace_address(&transport->local_addr, salt));
	ast_json_object_set(record, "peer", mdd_trace_address(peer, salt));
	cid = pjsip_msg_find_hdr(msg, PJSIP_H_CALL_ID, NULL);
	if (cid) {
		mdd_trace_label(label, "call", salt, &cid->id);
		mdd_trace_string(record, "call", label);
	}
	if (msg->type == PJSIP_REQUEST_MSG && (item = mdd_trace_uri(msg->line.req.uri, salt))) {
		ast_json_object_set(record, "uri", item);
	}
	contacts = ast_json_array_create();
	for (i = 0; i < 4; ++i) {
		contact = pjsip_msg_find_hdr(msg, PJSIP_H_CONTACT, contact ? contact->next : NULL);
		if (!contact) {
			break;
		}
		if (!contact->star && (item = mdd_trace_uri(contact->uri, salt))) {
			mdd_trace_int(item, "expires", contact->expires == PJSIP_EXPIRES_NOT_SPECIFIED ? -1LL : (long long) contact->expires);
			ast_json_array_append(contacts, item);
		}
	}
	ast_json_object_set(record, "contacts", contacts);
	expiry = pjsip_msg_find_hdr(msg, PJSIP_H_EXPIRES, NULL);
	if (expiry) {
		mdd_trace_int(record, "expires", expiry->ivalue);
	}
	mdd_trace_security(record, msg, "Security-Client", "security_client");
	mdd_trace_security(record, msg, "Security-Server", "security_server");
	mdd_trace_security(record, msg, "Security-Verify", "security_verify");
	serialized = ast_json_dump_string(record);
	ast_json_unref(record);
	if (!serialized) {
		return;
	}
	ast_mutex_lock(&mdd_trace_lock);
	snprintf(path, sizeof(path), MDD_TRACE_DIR "/sip.jsonl");
	if (!stat(path, &info) && info.st_size >= 512 * 1024) {
		snprintf(old, sizeof(old), MDD_TRACE_DIR "/sip.jsonl.1");
		rename(path, old);
	}
	file = fopen(path, "a");
	if (file) {
		chmod(path, 0600);
		fprintf(file, "%s\n", serialized);
		fclose(file);
	}
	ast_mutex_unlock(&mdd_trace_lock);
	ast_json_free(serialized);
}
