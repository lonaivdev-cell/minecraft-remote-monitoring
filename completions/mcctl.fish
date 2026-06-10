# fish completions for mcctl — Minecraft remote control & monitoring

set -l cmds init doctor status start stop restart kill console cmd save tps health profile purge stats logs backup props jvm player watchdog sync rcon dash gui

complete -c mcctl -f

# global flags
complete -c mcctl -l config -r -d 'config file (default ~/.config/mcctl/config.toml)'
complete -c mcctl -s v -l verbose -d 'increase verbosity (-v info, -vv debug)'
complete -c mcctl -l version -d 'print version'

# subcommands
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a init -d 'write a config template'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a doctor -d 'preflight checks'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a status -d 'full server status'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a start -d 'start the server'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a stop -d 'graceful stop'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a restart -d 'stop then start'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a kill -d 'emergency stop'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a console -d 'attach to live console'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a cmd -d 'run a console command'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a save -d 'save-all flush'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a tps -d 'spark TPS/MSPT'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a health -d 'spark health'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a profile -d 'spark profiler -> URL'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a purge -d 'GC purge + leak verdict'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a stats -d 'recent metric samples'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a logs -d 'tail/follow logs, crash reports'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a backup -d 'snapshot/rotate/pull/restore'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a props -d 'server.properties editor'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a jvm -d 'variables.txt: heap, JAVA'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a player -d 'whitelist/op/kick/ban'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a watchdog -d 'self-healing daemon'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a sync -d 'rsync config dir'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a rcon -d 'RCON channel status'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a dash -d 'live TUI dashboard'
complete -c mcctl -n "not __fish_seen_subcommand_from $cmds" -a gui -d 'GTK desktop app'

# per-subcommand
complete -c mcctl -n "__fish_seen_subcommand_from init" -l force -d 'overwrite existing config'
complete -c mcctl -n "__fish_seen_subcommand_from init" -l host -r
complete -c mcctl -n "__fish_seen_subcommand_from init" -l user -r
complete -c mcctl -n "__fish_seen_subcommand_from init" -l server-dir -r
complete -c mcctl -n "__fish_seen_subcommand_from doctor" -l fix -d 'apply safe fixes'
complete -c mcctl -n "__fish_seen_subcommand_from status tps health stats" -l json
complete -c mcctl -n "__fish_seen_subcommand_from status" -l fast -d 'skip spark/heap probes'
complete -c mcctl -n "__fish_seen_subcommand_from start" -l no-wait
complete -c mcctl -n "__fish_seen_subcommand_from stop restart" -l now -d 'skip player countdown'
complete -c mcctl -n "__fish_seen_subcommand_from stop restart" -l reason -r
complete -c mcctl -n "__fish_seen_subcommand_from kill" -l yes
complete -c mcctl -n "__fish_seen_subcommand_from profile" -l seconds -r
complete -c mcctl -n "__fish_seen_subcommand_from save" -l skip-if-down
complete -c mcctl -n "__fish_seen_subcommand_from logs" -s f -l follow
complete -c mcctl -n "__fish_seen_subcommand_from logs" -s n -l lines -r
complete -c mcctl -n "__fish_seen_subcommand_from logs; and not __fish_seen_subcommand_from crash" -a crash -d 'crash reports'
complete -c mcctl -n "__fish_seen_subcommand_from logs" -l list
complete -c mcctl -n "__fish_seen_subcommand_from logs" -l get -r
complete -c mcctl -n "__fish_seen_subcommand_from backup; and not __fish_seen_subcommand_from create list prune pull verify restore" -a 'create list prune pull verify restore'
complete -c mcctl -n "__fish_seen_subcommand_from backup; and __fish_seen_subcommand_from create" -l full -d 'whole instance, not just world'
complete -c mcctl -n "__fish_seen_subcommand_from backup; and __fish_seen_subcommand_from create prune" -l dry-run
complete -c mcctl -n "__fish_seen_subcommand_from backup; and __fish_seen_subcommand_from create" -l notify
complete -c mcctl -n "__fish_seen_subcommand_from backup; and __fish_seen_subcommand_from restore" -l yes
complete -c mcctl -n "__fish_seen_subcommand_from props; and not __fish_seen_subcommand_from list get set" -a 'list get set'
complete -c mcctl -n "__fish_seen_subcommand_from props; and __fish_seen_subcommand_from set" -l live -d 'also apply live when supported'
complete -c mcctl -n "__fish_seen_subcommand_from jvm; and not __fish_seen_subcommand_from show heap java" -a 'show heap java'
complete -c mcctl -n "__fish_seen_subcommand_from player; and not __fish_seen_subcommand_from list whitelist op deop kick ban pardon" -a 'list whitelist op deop kick ban pardon'
complete -c mcctl -n "__fish_seen_subcommand_from player; and __fish_seen_subcommand_from whitelist; and not __fish_seen_subcommand_from list add remove on off" -a 'list add remove on off'
complete -c mcctl -n "__fish_seen_subcommand_from watchdog; and not __fish_seen_subcommand_from run arm disarm status install" -a 'run arm disarm status install'
complete -c mcctl -n "__fish_seen_subcommand_from sync" -l pull -r -d 'server config/ -> local DEST'
complete -c mcctl -n "__fish_seen_subcommand_from sync" -l push -r -d 'local SRC -> server config/'
complete -c mcctl -n "__fish_seen_subcommand_from console" -s c -l command -r -d 'one-shot console command'
