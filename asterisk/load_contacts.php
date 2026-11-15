<?php
// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Darek Margas. See NOTICE.
// Load contacts into a FreePBX Contact Manager group (shared, "external"), replacing
// whatever the group held. Reads the JSON from tools/contacts_to_freepbx.py on stdin:
//
//     python tools/contacts_to_freepbx.py contacts.vcf | ssh root@pbx php /root/load_contacts.php imported
//
// Numbers are stored without a country (locale), so FreePBX keeps them as the plain
// digits Internode sends as caller ID (0491570156) and the caller lookup matches exactly.
// Contacts can be edited in Admin > Contact Manager afterwards; re-running replaces them.

include '/etc/freepbx.conf';

$name = $argv[1] ?? '';
if ($name === '') {
    fwrite(STDERR, "usage: php load_contacts.php <group name> < contacts.json\n");
    exit(1);
}
$entries = json_decode(stream_get_contents(STDIN), true);
if (!is_array($entries)) {
    fwrite(STDERR, "expected a JSON list of contacts on stdin\n");
    exit(1);
}

$cm = FreePBX::Contactmanager();
$groupid = null;
foreach ($cm->getGroups(true) as $group) {
    if ($group['name'] === $name && (int)$group['owner'] === -1) {
        $groupid = $group['id'];
    }
}
if ($groupid === null) {
    $result = $cm->addGroup($name, 'external', -1);
    if (empty($result['status'])) {
        fwrite(STDERR, "could not create group: {$result['message']}\n");
        exit(1);
    }
    $groupid = $result['id'];
    echo "created group $name (id $groupid)\n";
} else {
    $cm->deleteEntriesByGroupID($groupid, false);
    echo "emptied group $name (id $groupid)\n";
}

$numbers = 0;
foreach ($entries as $entry) {
    foreach ($entry['numbers'] as &$number) {
        $number += ['locale' => '', 'flags' => [], 'extension' => ''];
        $numbers++;
    }
    unset($number);
    $cm->addEntryByGroupID($groupid, $entry, false);
}
echo count($entries) . " contacts, $numbers numbers loaded into group $name (id $groupid)\n";
