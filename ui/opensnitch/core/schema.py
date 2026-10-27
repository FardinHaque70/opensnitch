"""Existing version-3 history schema, shared by Qt and sqlite3 adapters."""

SCHEMA_VERSION = 3
STATEMENTS = ('create table if not exists connections (time text, node text, action text, protocol text, src_ip text, '
 'src_port text, dst_ip text, dst_host text, dst_port text, uid text, pid text, process text, process_args '
 'text, process_cwd text, rule text, UNIQUE(node, action, protocol, src_ip, src_port, dst_ip, dst_port, uid, '
 'pid, process, process_args))',
 'CREATE INDEX IF NOT EXISTS time_index on connections (time)',
 'CREATE INDEX IF NOT EXISTS action_index on connections (action)',
 'CREATE INDEX IF NOT EXISTS protocol_index on connections (protocol)',
 'CREATE INDEX IF NOT EXISTS dst_host_index on connections (dst_host)',
 'CREATE INDEX IF NOT EXISTS process_index on connections (process)',
 'CREATE INDEX IF NOT EXISTS dst_ip_index on connections (dst_ip)',
 'CREATE INDEX IF NOT EXISTS dst_port_index on connections (dst_port)',
 'CREATE INDEX IF NOT EXISTS rule_index on connections (rule)',
 'CREATE INDEX IF NOT EXISTS node_index on connections (node)',
 'CREATE INDEX IF NOT EXISTS details_query_index on connections (process, process_args, uid, pid, dst_ip, '
 'dst_host, dst_port, action, node, protocol)',
 'create table if not exists nodes (addr text primary key,hostname text,daemon_version text,daemon_uptime '
 'text,daemon_rules text,cons text,cons_dropped text,version text,status text, last_connection text)',
 'create table if not exists rules (time text, node text, name text, enabled text, precedence text, action '
 'text, duration text, operator_type text, operator_sensitive text, operator_operand text, operator_data '
 'text, description text, nolog text, created text, UNIQUE(node, name))',
 'create table if not exists alerts (time text, node text, type text, action text, priority text, what text, '
 'body text, status int )',
 'create table if not exists sockets (id int primary key, last_seen text, node text, src_port text, src_ip '
 'text, dst_ip text, dst_port text, proto text, uid text, inode text, iface text, family text, state text, '
 'cookies text, rqueue text, wqueue text, expires text, retrans text, timer text, mark text, proc_pid text, '
 'proc_comm text, proc_path text, UNIQUE(node, src_port, src_ip, dst_ip, dst_port, proto, family, inode))',
 'CREATE INDEX IF NOT EXISTS sck_srcport_index on sockets (src_port)',
 'CREATE INDEX IF NOT EXISTS sck_dstip_index on sockets (dst_ip)',
 'CREATE INDEX IF NOT EXISTS sck_srcip_index on sockets (src_ip)',
 'CREATE INDEX IF NOT EXISTS sck_state_index on sockets (state)',
 'CREATE INDEX IF NOT EXISTS sck_comm_index on sockets (proc_comm)',
 'CREATE INDEX IF NOT EXISTS sck_path_index on sockets (proc_path)',
 'CREATE INDEX IF NOT EXISTS rules_index on rules (time)',
 'create table if not exists hosts (what text primary key, hits integer)',
 'create table if not exists procs (what text primary key, hits integer)',
 'create table if not exists addrs (what text primary key, hits integer)',
 'create table if not exists ports (what text primary key, hits integer)',
 'create table if not exists users (what text primary key, hits integer)')
