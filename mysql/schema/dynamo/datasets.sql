CREATE TABLE `datasets` (
  `id` int(10) unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(512) CHARACTER SET latin1 COLLATE latin1_general_cs NOT NULL,
  `status` enum('UNKNOWN','DELETED','DEPRECATED','INVALID','PRODUCTION','VALID','IGNORED') CHARACTER SET latin1 COLLATE latin1_general_ci DEFAULT NULL,
  `data_type` enum('UNKNOWN','PRODUCTION','TEST') CHARACTER SET latin1 COLLATE latin1_general_ci NOT NULL DEFAULT 'UNKNOWN',
  `software_version_id` int(10) unsigned NOT NULL DEFAULT 0,
  `last_update` datetime NOT NULL DEFAULT '0000-00-00 00:00:00',
  `is_open` tinyint(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1 CHECKSUM=1;
