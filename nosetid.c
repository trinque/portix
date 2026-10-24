/* portix: the store's identity model, at the libc boundary.
 *
 * A store path carries no owner and no setuid or setgid bit, nix's build sandbox
 * refuses a setuid/setgid chmod outright (seccomp, EPERM), and the build namespace
 * maps exactly one id. So every chmod here keeps its mode minus S_ISUID|S_ISGID,
 * every chown goes to 0 (an id of -1, "leave alone", stays -1), and every request
 * that lost something is appended to $PORTIX_NOSETID_LOG as
 *
 *	kind <TAB> request <TAB> absolute path
 *
 * Which rows name package content is decided after the merge, against the package's
 * own CONTENTS (pysrc/portix/emit.py); this file only refuses to lose the request.
 * A request that cannot be recorded aborts: the record is the admission.
 *
 * Every Portage process runs under this, so fperms, fowners, a build system's own
 * `chmod 4755` and Portage's setgid directories all meet the same rule. It is built
 * inside the root by the root's gcc: a store-built .so would link another libc, and
 * LD_PRELOAD, which Portage carries into every ebuild environment, would name a
 * store path.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

#define SETID ((mode_t)(S_ISUID | S_ISGID))
#define ID0(i) ((i) == (__typeof__(i))-1 ? (i) : 0)

static void *real(const char *name)
{
	void *fn = dlsym(RTLD_NEXT, name);

	if (fn == NULL) {
		fprintf(stderr, "portix nosetid: no real %s: %s\n", name, dlerror());
		abort();
	}
	return fn;
}

/* The absolute path a request names. `dirfd` and `path` mean what they mean to
 * fchmodat; a NULL or empty path is the descriptor itself. Symlinks are left
 * unresolved: an lchown is a statement about the link. */
static int locate(int dirfd, const char *path, char *out, size_t len)
{
	char base[PATH_MAX], link[64];
	ssize_t n;

	if (path != NULL && path[0] == '/')
		return snprintf(out, len, "%s", path) < (int)len ? 0 : -1;
	if (dirfd == AT_FDCWD) {
		if (getcwd(base, sizeof base) == NULL)
			return -1;
	} else {
		snprintf(link, sizeof link, "/proc/self/fd/%d", dirfd);
		n = readlink(link, base, sizeof base - 1);
		if (n < 0)
			return -1;
		base[n] = '\0';
	}
	if (path == NULL || path[0] == '\0')
		return snprintf(out, len, "%s", base) < (int)len ? 0 : -1;
	return snprintf(out, len, "%s/%s", base, path) < (int)len ? 0 : -1;
}

static void record(const char *kind, const char *request, int dirfd, const char *path)
{
	const char *log = getenv("PORTIX_NOSETID_LOG");
	char where[PATH_MAX], line[PATH_MAX + 64];
	int fd, n;

	if (log == NULL || *log == '\0')
		return;
	if (locate(dirfd, path, where, sizeof where) != 0)
		snprintf(where, sizeof where, "(unlocatable)");
	n = snprintf(line, sizeof line, "%s\t%s\t%s\n", kind, request, where);
	fd = open(log, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0644);
	if (fd < 0 || n < 0 || (size_t)n >= sizeof line || write(fd, line, (size_t)n) != n) {
		fprintf(stderr, "portix nosetid: cannot record %s %s %s in %s\n",
			kind, request, where, log);
		abort();
	}
	close(fd);
}

static void mode(mode_t m, int dirfd, const char *path)
{
	char request[8];

	if (m & SETID) {
		snprintf(request, sizeof request, "%04o", (unsigned)(m & 07777));
		record("mode", request, dirfd, path);
	}
}

static void owner(uid_t u, gid_t g, int dirfd, const char *path)
{
	char request[48];

	if (ID0(u) != u || ID0(g) != g) {
		/* -1 is printed as itself, never as 4294967295: a consumer would read the
		 * widened sentinel as a real id. */
		snprintf(request, sizeof request, "%ld:%ld",
			 u == (uid_t)-1 ? -1L : (long)u, g == (gid_t)-1 ? -1L : (long)g);
		record("owner", request, dirfd, path);
	}
}

int chmod(const char *path, mode_t m)
{
	static int (*next)(const char *, mode_t);

	if (next == NULL)
		next = real("chmod");
	mode(m, AT_FDCWD, path);
	return next(path, m & ~SETID);
}

int fchmod(int fd, mode_t m)
{
	static int (*next)(int, mode_t);

	if (next == NULL)
		next = real("fchmod");
	mode(m, fd, NULL);
	return next(fd, m & ~SETID);
}

int fchmodat(int dirfd, const char *path, mode_t m, int flags)
{
	static int (*next)(int, const char *, mode_t, int);

	if (next == NULL)
		next = real("fchmodat");
	mode(m, dirfd, path);
	return next(dirfd, path, m & ~SETID, flags);
}

int chown(const char *path, uid_t u, gid_t g)
{
	static int (*next)(const char *, uid_t, gid_t);

	if (next == NULL)
		next = real("chown");
	owner(u, g, AT_FDCWD, path);
	return next(path, ID0(u), ID0(g));
}

int lchown(const char *path, uid_t u, gid_t g)
{
	static int (*next)(const char *, uid_t, gid_t);

	if (next == NULL)
		next = real("lchown");
	owner(u, g, AT_FDCWD, path);
	return next(path, ID0(u), ID0(g));
}

int fchown(int fd, uid_t u, gid_t g)
{
	static int (*next)(int, uid_t, gid_t);

	if (next == NULL)
		next = real("fchown");
	owner(u, g, fd, NULL);
	return next(fd, ID0(u), ID0(g));
}

int fchownat(int dirfd, const char *path, uid_t u, gid_t g, int flags)
{
	static int (*next)(int, const char *, uid_t, gid_t, int);

	if (next == NULL)
		next = real("fchownat");
	owner(u, g, dirfd, path);
	return next(dirfd, path, ID0(u), ID0(g), flags);
}
