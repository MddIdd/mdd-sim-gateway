"""Add an expiring, metadata-only SIP recorder without enabling the raw SIP logger."""
import os
from pathlib import Path

SOURCE = Path(os.environ.get('AST_SRC', '/home/asterisk-build/asterisk')) / 'res/res_pjsip_logger.c'
MARKER = 'PATCH mdd_vowifi_diagnostics'

def patch(source):
    if MARKER in source:
        return source
    implementation = Path(__file__).with_suffix('.c').read_text()
    anchor = 'static pj_status_t logging_on_tx_msg(pjsip_tx_data *tdata)\n'
    if source.count(anchor) != 1:
        raise ValueError('PJSIP logger transmit hook missing or ambiguous')
    source = source.replace(anchor, '/* ' + MARKER + ' */\n' + implementation + '\n' + anchor, 1)
    anchor = 'static pj_status_t logging_on_tx_msg(pjsip_tx_data *tdata)\n{\n\tchar buffer[AST_SOCKADDR_BUFLEN];\n'
    if source.count(anchor) != 1:
        raise ValueError('PJSIP logger transmit body missing or ambiguous')
    source = source.replace(anchor, anchor + '\n\tmdd_trace_sip(tdata->msg, tdata->tp_info.transport, &tdata->tp_info.dst_addr, "tx");\n', 1)
    anchor = '\tif (!rdata->msg_info.msg) {\n\t\treturn PJ_FALSE;\n\t}\n'
    if source.count(anchor) != 1:
        raise ValueError('PJSIP logger receive body missing or ambiguous')
    return source.replace(anchor, anchor + '\n\tmdd_trace_sip(rdata->msg_info.msg, rdata->tp_info.transport, &rdata->pkt_info.src_addr, "rx");\n', 1)

if __name__ == '__main__':
    original = SOURCE.read_text()
    SOURCE.write_text(patch(original))
    print('installed expiring, metadata-only VoWiFi SIP recorder')
