# Copyright (c) 2024, AgriTheory and contributors
# For license information, please see license.txt

import base64
import json
import os
import re
import subprocess
import types
import uuid
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import quote, unquote
from urllib.request import urlopen
import tempfile

import frappe
from boto3.exceptions import S3UploadFailedError
from boto3.session import Session
from botocore.config import Config
from botocore.exceptions import ClientError
from frappe import DoesNotExistError, _
from frappe.core.doctype.file.file import File, get_files_path
from frappe.core.doctype.file.utils import decode_file_content, get_content_hash
from frappe.model.rename_doc import rename_doc
from frappe.utils import cint, get_datetime, get_url
from frappe.utils.image import optimize_image, strip_exif_data
from magic import from_buffer
from PIL import UnidentifiedImageError
from werkzeug.datastructures import FileStorage

FILE_URL = "/api/method/retrieve?key={path}"
URL_PREFIXES = ("http://", "https://", "/api/method/retrieve")


class CloudStorageFile(File):
	@File.is_remote_file.getter
	def is_remote_file(self) -> bool:
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: is_remote_file
		"""
		if self.file_url:  # type: ignore
			return self.file_url.startswith(URL_PREFIXES)  # type: ignore
		return not self.content

	def validate(self) -> None:
		"""
		HASH: 69a495579a729909f4df7a45855165eee4a208f4
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: validate
		"""
		self.associate_files()
		if self.flags.cloud_storage or self.flags.ignore_file_validate:
			return
		if not self.is_remote_file:
			self.custom_validate()
		else:
			self.validate_file_url()

	def custom_validate(self):
		if self.is_folder:
			return

		# Ensure correct formatting and type
		self.file_url = unquote(self.file_url) if self.file_url else ""

		self.validate_attachment_references()

		# when dict is passed to get_doc for creation of new_doc, is_new returns None
		# this case is handled inside handle_is_private_changed
		if not self.is_new() and self.has_value_changed("is_private"):
			self.handle_is_private_changed()

		self.validate_file_path()
		self.validate_file_url()

		config = frappe.conf.cloud_storage_settings
		if not config or config.get("use_local"):
			self.validate_file_on_disk()

		self.file_size = frappe.form_dict.file_size or self.file_size

	def save_file(
		self,
		content: bytes | str | None = None,
		decode=False,
		ignore_existing_file_check=False,
		overwrite=False,
	):
		# write_file (cloud or local) owns naming and dedup; skip Frappe's filesystem rename.
		return super().save_file(
			content=content,
			decode=decode,
			ignore_existing_file_check=ignore_existing_file_check,
			overwrite=True,
		)

	def after_insert(self) -> File:
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: after_insert
		"""
		if self.attached_to_doctype and self.attached_to_name and not self.file_association:  # type: ignore
			current_file_url = self.file_url or ""
			if not self.content_hash and "/api/method/retrieve" in current_file_url:  # type: ignore
				associated_doc = frappe.get_value("File", {"file_url": self.file_url}, "name")  # type: ignore
			else:
				associated_doc = frappe.get_value(
					"File",
					{"content_hash": self.content_hash, "name": ["!=", self.name], "is_folder": False},  # type: ignore
				)
			if associated_doc and associated_doc != self.name:
				# Clear only so the merge/delete hook does not drop the remote object.
				# Restore the retrieve URL on the surviving row and on this document so
				# the upload response still has /api/method/retrieve?key=...
				object_path = object_path_for_file(self)
				self.db_set(
					"file_url", ""
				)  # this is done to prevent deletion of the remote file with the delete_file hook
				rename_doc(
					self.doctype,
					self.name,
					associated_doc,
					merge=True,
					force=True,
					show_alert=False,
					ignore_permissions=True,
					# validate=False,
				)
				restored = restore_retrieve_file_url(associated_doc, object_path)
				if restored:
					self.file_url = restored
			if associated_doc and not self.s3_key:
				s3_key = path_from_file_url(self.file_url)
				if s3_key:
					frappe.db.set_value("File", associated_doc, "s3_key", s3_key)
					frappe.db.commit()
		elif self.attached_to_doctype and self.attached_to_name and self.file_name:  # type: ignore
			associated_doc = frappe.db.get_value(
				"File",
				{
					"file_name": ["=", self.file_name],
					"content_hash": self.content_hash,
					"name": ["!=", self.name],
					"is_folder": False,
				},
				"name",  # type: ignore
			)
			if associated_doc:
				doc = frappe.get_doc("File", associated_doc)
				# Merge file associations
				doc.append(
					"file_association",
					add_child_file_association(
						self.attached_to_doctype,  # type: ignore
						self.attached_to_name,  # type: ignore
					),
				)
				already_linked = any(version.version == self.content_hash for version in self.versions)
				if not already_linked:
					doc.append(
						"versions",
						{
							"version": str(self.content_hash),
							"user": frappe.session.user,
							"timestamp": get_datetime(),
						},
					)
				object_path = object_path_for_file(self) or object_path_for_file(doc)
				ensure_retrieve_file_url(doc, object_path)
				doc.save()
				# Clear before delete so the remote object shared with the surviving row is kept.
				self.db_set("file_url", "")
				frappe.delete_doc("File", self.name, ignore_permissions=True)
				restored = restore_retrieve_file_url(associated_doc, object_path)
				if restored:
					self.file_url = restored

	def on_trash(self) -> None:
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: on_trash
		"""
		user_roles = frappe.get_roles(frappe.session.user)
		if (
			frappe.session.user != "Administrator"
			and "System Manager" not in user_roles
			and (frappe.get_value(self.attached_to_doctype, self.attached_to_name, "docstatus") == 1)  # type: ignore
		):
			frappe.throw(
				_("This file is attached to a submitted document and cannot be deleted"),
				frappe.PermissionError,
			)
		if self.is_home_folder or self.is_attachments_folder:
			frappe.throw(_("Cannot delete Home and Attachments folders"))
		if len(self.file_association) > 0:
			return
		self.validate_empty_folder()
		self._delete_file_on_disk()
		# even though the code is unreachable, we're keeping it here for reference
		if not self.is_folder and len(self.file_association) > 0:
			self.add_comment_in_reference_doc("Attachment Removed", _("Removed {0}").format(self.file_name))

	def associate_files(
		self, attached_to_doctype: str | None = None, attached_to_name: str | None = None
	) -> None:
		attached_to_doctype = attached_to_doctype or self.attached_to_doctype  # type: ignore
		attached_to_name = attached_to_name or self.attached_to_name  # type: ignore

		if not attached_to_doctype:
			return
		ensure_retrieve_file_url(self)
		if not self.content_hash and "/api/method/retrieve" in self.file_url:  # type: ignore
			associated_doc = frappe.get_value("File", {"file_url": self.file_url}, "name")  # type: ignore
		else:
			associated_doc = frappe.get_value(
				"File",
				{"content_hash": self.content_hash, "name": ["!=", self.name], "is_folder": False},  # type: ignore
			)
		if associated_doc and associated_doc != self.name:
			existing_file = frappe.get_doc("File", associated_doc)
			existing_file.attached_to_doctype = attached_to_doctype
			existing_file.attached_to_name = attached_to_name
			existing_file.append(
				"file_association",
				add_child_file_association(attached_to_doctype, attached_to_name),
			)
			ensure_retrieve_file_url(
				existing_file, object_path_for_file(self) or object_path_for_file(existing_file)
			)
			existing_file.save()
		else:
			if self.file_association:
				already_linked = any(
					assoc.link_doctype == attached_to_doctype and assoc.link_name == attached_to_name
					for assoc in self.file_association
				)
				if not already_linked:
					self.append(
						"file_association",
						add_child_file_association(attached_to_doctype, attached_to_name),
					)
			else:
				self.append(
					"file_association",
					add_child_file_association(attached_to_doctype, attached_to_name),
				)

	def add_file_version(self, version_id):
		self.append(
			"versions",
			{
				"version": str(version_id),
				"user": frappe.session.user,
				"timestamp": get_datetime(),
			},
		)
		if not self.is_new():
			# File already exists in DB (filename-conflict path in write_file).
			# Frappe's save lifecycle won't persist this file's child tables,
			# so we insert the version record directly.
			frappe.get_doc(
				{
					"doctype": "File Version",
					"parent": self.name,
					"parenttype": "File",
					"parentfield": "versions",
					"version": str(version_id),
					"user": frappe.session.user,
					"timestamp": get_datetime(),
				}
			).insert(ignore_permissions=True)

	def remove_file_association(self, dt: str, dn: str) -> None:
		if len(self.file_association) <= 1:
			frappe.db.delete("File Association", {"parent": self.name})
			frappe.db.commit()
			self.delete()
			return
		to_remove = []
		for idx, row in enumerate(self.file_association):
			if row.link_doctype == dt and row.link_name == dn:
				to_remove.append(row)
				if row.link_doctype == self.attached_to_doctype and row.link_name == self.attached_to_name:  # type: ignore
					# calculate the index of the next file association in the list, looping to the start if already at the end
					next_idx = (idx + 1) % len(self.file_association)
					next_file_association = self.file_association[next_idx]
					self.attached_to_doctype = next_file_association.link_doctype
					self.attached_to_name = next_file_association.link_name
		for row in to_remove:
			self.remove(row)
		for idx, association in enumerate(self.file_association, start=1):
			association.idx = idx
		self.save()

	@frappe.whitelist()
	def get_content(self) -> bytes:
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: get_content
		"""
		if self.is_folder:
			frappe.throw(_("Cannot get file contents of a Folder"))

		if self.get("content"):
			self._content = self.content
			if self.decode:  # type: ignore
				self._content = decode_file_content(self._content)
				self.decode = False
			# self.content = None # TODO: This needs to happen; make it happen somehow
			return self._content

		if self.file_url:
			self.validate_file_url()

		if self.file_url.startswith("/api/method/retrieve"):
			client = get_cloud_storage_client()
			file_object = client.get_object(Bucket=client.bucket, Key=self.s3_key)
			self._content = file_object.get("Body").read()
		elif self.file_url.startswith("http://") or self.file_url.startswith("https://"):
			self._content = urlopen(self.file_url).read()
		else:
			if not self.is_private:
				file_path = frappe.get_site_path("public", "files", self.file_name)
			else:
				file_path = frappe.get_site_path("private", "files", self.file_name)
			with open(file_path, mode="rb") as f:
				self._content = f.read()
				try:
					# for plain text files
					self._content = self._content.decode()
				except UnicodeDecodeError:
					# for .png, .jpg, etc
					pass
		return self._content

	def get_full_path(self):
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: get_full_path
		"""
		"""Returns file path from given file name"""

		file_path = self.file_url or self.file_name

		site_url = get_url()
		if "/files/" in file_path and file_path.startswith(site_url):
			file_path = file_path.split(site_url, 1)[1]

		if "/" not in file_path:
			if self.is_private:
				file_path = f"/private/files/{file_path}"
			else:
				file_path = f"/files/{file_path}"

		if file_path.startswith("/private/files/"):
			file_path = get_files_path(*file_path.split("/private/files/", 1)[1].split("/"), is_private=1)

		elif file_path.startswith("/files/"):
			file_path = get_files_path(*file_path.split("/files/", 1)[1].split("/"))

		elif file_path.startswith(URL_PREFIXES):
			pass

		elif not self.file_url:
			frappe.throw(_("There is some problem with the file url: {0}").format(file_path))

		if not is_safe_path(file_path):
			frappe.throw(_("Cannot access file path {0}").format(file_path))

		if os.path.sep in self.file_name:
			frappe.throw(_("File name cannot have {0}").format(os.path.sep))

		return file_path

	@frappe.whitelist()
	def get_pdf_preview(self):
		if self.is_folder:
			frappe.throw(_("Cannot get file contents of a Folder"))

		ext = self.file_name.split(".")[-1].lower()

		if self.file_url.startswith("/api/method/retrieve"):
			client = get_cloud_storage_client()
			ppt_s3_key = self.s3_key

			with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as temp_file:
				file_bytes = client.get_object(Bucket=client.bucket, Key=ppt_s3_key)["Body"].read()

				temp_file.write(file_bytes)
				temp_file.flush()

				file_path = Path(temp_file.name)

			return convert_to_pdf_base64(file_path)

		else:
			if not self.is_private:
				file_path = Path(frappe.get_site_path("public", "files", self.file_name))
			else:
				file_path = Path(frappe.get_site_path("private", "files", self.file_name))

			return convert_to_pdf_base64(file_path)


def convert_to_pdf_base64(file_path: Path):
	with tempfile.TemporaryDirectory() as tmpdir:
		tmpdir_path = Path(tmpdir)

		subprocess.run(
			[
				"libreoffice",
				"--headless",
				"--convert-to",
				"pdf",
				"--outdir",
				str(tmpdir_path),
				str(file_path),
			],
			check=True,
		)

		pdf_filename = file_path.with_suffix(".pdf").name
		pdf_path = tmpdir_path / pdf_filename

		with open(pdf_path, "rb") as f:
			pdf_bytes = f.read()
			return base64.b64encode(pdf_bytes).decode("utf-8")


def is_safe_path(path: str) -> bool:
	if path.startswith(URL_PREFIXES):
		return True

	basedir = frappe.get_site_path()
	# ref: https://docs.python.org/3/library/os.path.html#os.path.commonpath
	matchpath = os.path.abspath(path)
	basedir = os.path.abspath(basedir)

	return basedir == os.path.commonpath((basedir, matchpath))


@frappe.whitelist()
def get_sharing_link(docname: str, reset: str | bool | None = None) -> str:
	if isinstance(reset, str):
		reset = json.loads(reset)
	doc = frappe.get_doc("File", docname)
	if doc.is_private:
		frappe.has_permission(
			doctype="File", ptype="share", doc=doc, user=frappe.session.user, throw=True
		)
	if reset or not doc.sharing_link:
		doc.db_set("sharing_link", str(uuid.uuid4().int >> 64))
	return f"{get_url()}/api/method/share?key={doc.sharing_link}"


def strip_special_chars(file_name: str) -> str:
	regex = re.compile(r"[^\w\s_.()-]")
	return regex.sub("", file_name)


@frappe.whitelist()
def get_cloud_storage_client():
	validate_config()

	config: dict = frappe.conf.cloud_storage_settings
	session = Session(
		aws_access_key_id=config.get("access_key"),
		aws_secret_access_key=config.get("secret"),
		region_name=config.get("region"),
	)
	client = session.client(
		"s3", endpoint_url=config.get("endpoint_url"), config=Config(signature_version="s3v4")
	)
	client.bucket = config.get("bucket")
	client.folder = config.get("folder", None)
	client.expiration = config.get("expiration", 120)
	client.get_presigned_url = types.MethodType(get_presigned_url, client)
	client.get_sharing_url = types.MethodType(get_sharing_url, client)

	return client


def validate_config() -> None:
	config: dict = frappe.conf.cloud_storage_settings

	if not config:
		frappe.throw(
			msg=_("Please setup cloud storage settings in your site configuration file"),
			title=_("Cloud storage not configured"),
		)

	if not config.get("access_key"):
		frappe.throw(
			msg=_("Please setup access_key in your site configuration file"),
			title=_("Cloud storage access key not configured"),
		)

	if not config.get("secret"):
		frappe.throw(
			msg=_("Please setup secret in your site configuration file"),
			title=_("Cloud storage secret not configured"),
		)

	if not config.get("region"):
		frappe.throw(
			msg=_("Please setup region in your site configuration file"),
			title=_("Cloud storage region not configured"),
		)

	if not config.get("bucket"):
		frappe.throw(
			msg=_("Please setup bucket in your site configuration file"),
			title=_("Cloud storage bucket not configured"),
		)


def file_is_private(is_private) -> bool:
	"""Check and string \"0\" are public. Only a real private flag needs a File read check."""
	return bool(cint(is_private))


def cloud_storage_active() -> bool:
	settings = frappe.conf.get("cloud_storage_settings")
	return bool(settings) and not settings.get("use_local")


def path_from_file_url(file_url: str | None) -> str | None:
	if not file_url or "key=" not in file_url:
		return None
	key = unquote(file_url.split("key=", 1)[1].split("&", 1)[0]).strip()
	return key or None


def object_path_for_file(file, fallback_path: str | None = None) -> str | None:
	path = (
		getattr(file, "s3_key", None)
		or path_from_file_url(getattr(file, "file_url", None))
		or fallback_path
	)
	if path:
		return path
	if not cloud_storage_active():
		return None
	folder = (frappe.conf.get("cloud_storage_settings") or {}).get("folder")
	return get_file_path(file, folder) or None


def ensure_retrieve_file_url(file, path: str | None = None) -> str:
	"""Keep a cloud File row addressable via /api/method/retrieve?key={path}.

	Sets the attribute so the upload response is never file_url \"\". Persists when the
	row already exists. Private files still get a retrieve URL; retrieve() enforces read.
	Also keeps ``s3_key`` in sync — retrieve used to find a private sibling by s3_key
	while the public upload only had file_url, then 403 LMS students.
	"""
	if not cloud_storage_active():
		return getattr(file, "file_url", None) or ""

	path = path or object_path_for_file(file)
	current = getattr(file, "file_url", None) or ""
	if current.startswith("/api/method/retrieve") and "key=" in current:
		url = current
		if not path:
			path = path_from_file_url(current)
	else:
		url = FILE_URL.format(path=path) if path else ""
		if not url:
			return current
		file.file_url = url

	if path:
		file.s3_key = path

	name = getattr(file, "name", None)
	if name and not file.is_new() and frappe.db.exists("File", name):
		values = {}
		if url:
			values["file_url"] = url
		if path:
			values["s3_key"] = path
		if values:
			frappe.db.set_value("File", name, values, update_modified=False)
	return url or current


def restore_retrieve_file_url(file_name: str, path: str | None = None) -> str:
	"""Put the retrieve URL back on the row clients still see after a merge clears it."""
	if not file_name or not cloud_storage_active():
		return FILE_URL.format(path=path) if path else ""
	if not frappe.db.exists("File", file_name):
		return FILE_URL.format(path=path) if path else ""

	current = frappe.db.get_value("File", file_name, ["file_url", "s3_key"], as_dict=True) or {}
	existing = current.get("file_url") or ""
	if existing.startswith("/api/method/retrieve") and "key=" in existing:
		return existing

	path = path or current.get("s3_key")
	if not path:
		path = object_path_for_file(frappe.get_doc("File", file_name))
	url = FILE_URL.format(path=path) if path else ""
	if not url:
		return ""

	frappe.db.set_value("File", file_name, "file_url", url, update_modified=False)
	if path and not current.get("s3_key"):
		frappe.db.set_value("File", file_name, "s3_key", path, update_modified=False)
	return url


def _row_get(row, field, default=None):
	if row is None:
		return default
	if isinstance(row, dict):
		return row.get(field, default)
	return getattr(row, field, default)


def prefer_public_file(rows):
	"""When several File rows match, serve a public sibling instead of a private one."""
	if not rows:
		return None
	for row in rows:
		if not file_is_private(_row_get(row, "is_private")):
			return row
	return rows[0]


def _retrieve_lookup_keys(key: str) -> list[str]:
	raw = (key or "").strip()
	if not raw:
		return []
	decoded = unquote(raw)
	keys = [raw]
	if decoded and decoded not in keys:
		keys.append(decoded)
	return keys


def resolve_file_for_retrieve(key: str):
	"""Find the File to authorize for retrieve.

	Collect matches from s3_key, file_url, and File.name, then prefer is_private=0.
	A private duplicate often keeps ``s3_key`` while a new public upload only has
	``file_url`` (merge / empty s3_key). Looking up s3_key alone used to pick the
	private row and 403 LMS students with "No permission for File <hash>".
	"""
	fields = ["name", "is_private", "s3_key", "file_url"]
	keys = _retrieve_lookup_keys(key)
	if not keys:
		return None

	def matching(filters: dict):
		return frappe.get_all("File", filters=filters, fields=fields, limit_page_length=20)

	candidates: list = []
	seen: set[str] = set()

	def add_rows(rows):
		for row in rows or []:
			name = _row_get(row, "name")
			if not name or name in seen:
				continue
			seen.add(name)
			candidates.append(row)

	# Public hits across every lookup key first.
	for candidate in keys:
		add_rows(matching({"s3_key": candidate, "is_private": 0}))
		add_rows(matching({"file_url": ["like", f"%{candidate}%"], "is_private": 0}))
		add_rows(matching({"name": candidate, "is_private": 0}))

	chosen = prefer_public_file(candidates)
	if chosen and not file_is_private(_row_get(chosen, "is_private")):
		return chosen

	# No public row — include private matches for owner / desk retrieve.
	for candidate in keys:
		add_rows(matching({"s3_key": candidate}))
		add_rows(matching({"file_url": ["like", f"%{candidate}%"]}))
		add_rows(matching({"name": candidate}))

	chosen = prefer_public_file(candidates)
	if not chosen:
		return None

	if not file_is_private(_row_get(chosen, "is_private")):
		return chosen

	# Private primary hit: still prefer a public sibling that only has file_url.
	object_key = _row_get(chosen, "s3_key") or path_from_file_url(_row_get(chosen, "file_url")) or key
	sibling_keys = _retrieve_lookup_keys(object_key)
	for candidate in sibling_keys:
		add_rows(matching({"s3_key": candidate, "is_private": 0}))
		add_rows(matching({"file_url": ["like", f"%{candidate}%"], "is_private": 0}))
	public = prefer_public_file(candidates)
	if public and not file_is_private(_row_get(public, "is_private")):
		return public
	return chosen


def get_presigned_url(client, key: str):
	file = resolve_file_for_retrieve(key)
	if not file:
		raise DoesNotExistError(frappe._("The file you are looking for is not available"))

	object_key = _row_get(file, "s3_key") or path_from_file_url(_row_get(file, "file_url")) or key
	is_private = file_is_private(_row_get(file, "is_private"))
	expiration = client.expiration if is_private else None

	# Public media (including is_private "0") must not hit File role permissions.
	# Private instructor files keep the existing read check.
	if is_private:
		file_doc = frappe.get_doc("File", _row_get(file, "name"))
		frappe.has_permission(
			doctype="File", ptype="read", doc=file_doc, user=frappe.session.user, throw=True
		)

	return client.generate_presigned_url(
		ClientMethod="get_object",
		Params={"Bucket": client.bucket, "Key": object_key},
		ExpiresIn=expiration,
	)


def get_sharing_url(client, key: str) -> str:
	file = frappe.get_value("File", {"sharing_link": key}, ["name", "s3_key"], as_dict=True)
	if not file:
		raise DoesNotExistError(frappe._("The file you are looking for is not available"))

	return client.generate_presigned_url(
		ClientMethod="get_object", Params={"Bucket": client.bucket, "Key": file.s3_key}
	)


def upload_file(file: File) -> File:
	client = get_cloud_storage_client()
	path = get_file_path(file, client.folder)
	# Set the attribute even when the row is not inserted yet (before_insert). db_set
	# alone can leave the serialized upload response with file_url "".
	file.file_url = FILE_URL.format(path=path)
	if not file.is_new() and file.name and frappe.db.exists("File", file.name):
		file.db_set("file_url", file.file_url)
	content_type = file.content_type or from_buffer(file.content, mime=True)
	version_id = None
	try:
		response = client.put_object(
			Body=file.content, Bucket=client.bucket, Key=path, ContentType=content_type
		)
		version_id = response.get("VersionId") or file.content_hash
		file.associate_files(file.attached_to_doctype, file.attached_to_name)
	except S3UploadFailedError:
		frappe.throw(_("File Upload Failed. Please try again."))
	except Exception as e:
		frappe.log_error("File Upload Error", e)
	if version_id:
		file.add_file_version(version_id)
	file.s3_key = path
	if not file.is_new() and file.name and frappe.db.exists("File", file.name):
		file.db_set("s3_key", path)
	ensure_retrieve_file_url(file, path)
	return file


def get_file_path(file: File, folder: str | None = None) -> str:
	custom_storage_path_generator = frappe.get_hooks("cloud_storage_path_generator")

	if custom_storage_path_generator and len(custom_storage_path_generator) > 0:
		try:
			generator_fn = frappe.get_attr(custom_storage_path_generator[0])
			return generator_fn(file, folder)
		except Exception as e:
			frappe.log_error(f"Custom path generator failed: {str(e)}", "Cloud Storage Path Error")

	config = frappe.conf.get("cloud_storage_settings", {})
	if config.get("use_legacy_paths", True):
		return _legacy_get_file_path(file, folder)

	if folder:
		return f"{folder}/{file.file_name}"

	return file.file_name


def _legacy_get_file_path(file: File, folder: str | None = None) -> str:
	parent_doctype = file.attached_to_doctype or "No Doctype"

	attached_to_name = ""
	if file.attached_to_name:
		attached_to_name = file.attached_to_name.replace("#", "%23")

	fragments = [
		folder,
		parent_doctype,
		attached_to_name,
		file.file_name.replace("#", "%23"),
	]

	valid_fragments: list[str] = list(filter(None, fragments))
	path = "/".join(valid_fragments)
	return path


def get_file_content_hash(content, content_type):
	try:
		stripped_content = strip_exif_data(content, content_type)
		return get_content_hash(stripped_content)
	except UnidentifiedImageError:
		return get_content_hash(content)


@frappe.whitelist()
def write_file(file: File, remove_spaces_in_file_name: bool = True) -> File:
	if not frappe.conf.cloud_storage_settings or frappe.conf.cloud_storage_settings.get(
		"use_local", False
	):
		file.save_file_on_filesystem()
		return file

	if file.attached_to_doctype == "Data Import":
		file.save_file_on_filesystem()
		return file

	# if a hash-conflict is found, update the existing document with a new file association
	existing_file_hashes = frappe.get_all(
		"File",
		filters={"name": ["!=", file.name], "content_hash": file.content_hash},
		pluck="name",
	)

	if existing_file_hashes:
		file_doc: File = frappe.get_doc("File", existing_file_hashes[0])
		file_doc.associate_files(file.attached_to_doctype, file.attached_to_name)
		file_doc.save()
		path = object_path_for_file(file_doc) or object_path_for_file(file)
		ensure_retrieve_file_url(file_doc, path)
		# Upload ignores write_file's return and serializes this document. Copy the URL.
		ensure_retrieve_file_url(file, path)
		return file_doc

	# if a filename-conflict is found, update the existing document with a new version instead
	existing_file_names = frappe.get_all(
		"File", filters={"name": ["!=", file.name], "file_name": file.file_name}, pluck="name"
	)

	incoming = file
	if existing_file_names:
		file_doc = frappe.get_doc("File", existing_file_names[0])
		file_doc.update(
			{
				"content": file.content,
				"content_hash": file.content_hash,
				"content_type": file.content_type,
			}
		)
		file_doc.associate_files(file.attached_to_doctype, file.attached_to_name)
		file = file_doc

	if remove_spaces_in_file_name:
		file.file_name = file.file_name.replace(" ", "_")

	file.file_name = strip_special_chars(file.file_name)
	file.flags.cloud_storage = True
	uploaded = upload_file(file)
	path = object_path_for_file(uploaded)
	ensure_retrieve_file_url(uploaded, path)
	if incoming is not uploaded:
		ensure_retrieve_file_url(incoming, path)
	return uploaded


@frappe.whitelist()
def delete_file(file: File, **kwargs) -> File:
	if not frappe.conf.cloud_storage_settings or frappe.conf.cloud_storage_settings.get(
		"use_local", False
	):
		file.delete_file_from_filesystem()
		return file

	if file.is_folder:
		return file

	if file.file_url and "?key=" in file.file_url:
		key = file.file_url.split("?key=")[1]
		if key:
			client = get_cloud_storage_client()
			try:
				client.delete_object(Bucket=client.bucket, Key=key)
			except ClientError:
				frappe.throw(_("Access denied: Could not delete file"))
			except Exception as e:
				print(f"EXCEPTION: {e}")
				frappe.log_error(str(e), "Cloud Storage Error: Could not delete file")

	return file


@frappe.whitelist()
def validate_file_content(*args, **kwargs):
	matched_files = []
	files = frappe.request.files

	if "file" in files:
		file: FileStorage = files["file"]
		content_type = guess_type(file.filename)[0]

		# validate filename
		file_name = file.filename
		existing_files_by_name = frappe.get_all(
			"File", filters={"file_name": file_name}, pluck="file_name"
		)

		# validate content hash
		file.stream.seek(0)
		content = file.stream.read()
		content_hash = get_file_content_hash(content, content_type)

		existing_files_by_hash = frappe.get_all(
			"File", filters={"content_hash": content_hash}, pluck="file_name"
		)

		# if no files are found by name or hash, and if the file is an image, match against optimized content
		if not existing_files_by_hash and content_type.startswith("image/"):
			optimized_content = optimize_image(content, content_type)
			optimized_content_hash = get_file_content_hash(optimized_content, content_type)
			existing_files_by_hash = frappe.get_all(
				"File", filters={"content_hash": optimized_content_hash}, pluck="file_name"
			)

		# build a list of matched files
		matched_files = list(set(existing_files_by_name + existing_files_by_hash))

	return {
		"filename_exists": len(existing_files_by_name) > 0,
		"content_exists": len(existing_files_by_hash) > 0,
		"matched_files": matched_files,
	}


@frappe.whitelist(allow_guest=True)
def retrieve(key: str) -> None:
	if key:
		client = get_cloud_storage_client()
		signed_url = client.get_presigned_url(key)
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = signed_url

	frappe.local.response["body"] = "Key not found"


@frappe.whitelist(allow_guest=True)
def share(key: str) -> None:
	if key:
		client = get_cloud_storage_client()
		signed_url = client.get_sharing_url(key)
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = signed_url

	frappe.local.response["body"] = "Key not found"


@frappe.whitelist(methods=["DELETE", "POST"])
def remove_attach():
	"""
	HASH: 354843a7a42249f2bd1a96706a9ae70dedc610ff
	REPO: https://github.com/frappe/frappe
	PATH: frappe/desk/form/utils.py
	METHOD: remove_attach
	"""
	fid = frappe.form_dict.get("fid")
	dt = frappe.form_dict.get("dt")
	dn = frappe.form_dict.get("dn")
	if not all([fid, dt, dn]):
		return
	doc = frappe.get_doc("File", fid)
	doc.remove_file_association(dt, dn)


def add_child_file_association(attached_to_doctype, attached_to_name):
	return {
		"link_doctype": attached_to_doctype,
		"link_name": attached_to_name,
		"user": frappe.session.user,
		"timestamp": get_datetime(),
	}
