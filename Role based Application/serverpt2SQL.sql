USE user_management;
DELETE FROM users WHERE id > 0;

ALTER TABLE users
ADD COLUMN password VARCHAR(255) NOT NULL,
ADD COLUMN role ENUM('Admin', 'Customer') NOT NULL;

ALTER TABLE users AUTO_INCREMENT = 1;

INSERT INTO users (name, email, password, role) VALUES
('Admin1', 'admin1@umd.edu', 'scrypt:32768:8:1$bXUBxHtvlSK7PZQb$dcff788f0d219f13567a752be5cb6bcdd983c69f860322f901c3fe685022dffebb38b4f4688a0079e16667dc14b16cce645e6a94f51e704a1bf3b27b99a7b164', 'Admin'),
('Admin2', 'admin2@umd.edu', 'scrypt:32768:8:1$bXUBxHtvlSK7PZQb$dcff788f0d219f13567a752be5cb6bcdd983c69f860322f901c3fe685022dffebb38b4f4688a0079e16667dc14b16cce645e6a94f51e704a1bf3b27b99a7b164', 'Admin'),
('Student', 'student@umd.edu', 'scrypt:32768:8:1$bXUBxHtvlSK7PZQb$dcff788f0d219f13567a752be5cb6bcdd983c69f860322f901c3fe685022dffebb38b4f4688a0079e16667dc14b16cce645e6a94f51e704a1bf3b27b99a7b164', 'Customer') ;

#scrypt:32768:8:1$bXUBxHtvlSK7PZQb$dcff788f0d219f13567a752be5cb6bcdd983c69f860322f901c3fe685022dffebb38b4f4688a0079e16667dc14b16cce645e6a94f51e704a1bf3b27b99a7b164

SELECT * FROM users;
